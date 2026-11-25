#include "pm/backend.hpp"
#include "pm/platform.hpp"
#include "pm/dvfs.hpp"
#include "pm/dvfs_sysfs.hpp"
#include <chrono>
#include <csignal>
#include <iostream>
#include <iomanip>
#include <map>
#include <thread>
using Clock = std::chrono::steady_clock;
using namespace std::chrono;
static volatile std::sig_atomic_t stop = 0;
static void on_signal(int) { stop = 1; }
static double ms(Clock::duration d) { return duration<double, std::milli>(d).count(); }
using Profiles = std::map<std::string, std::vector<pm::Cost>>;
static Profiles profile(const std::string& path) {
    Profiles out;
    if (path.empty()) return out;
    std::ifstream f(path);
    std::string line;
    if (!std::getline(f,line)) throw std::runtime_error("Empty profile");
    if (!line.empty() && line.back() == '\r') line.pop_back();
    const bool tagged = line == "backend,state,cap_khz,p95_ms,energy_mj,power_w,workload";
    if (!tagged && line != "backend,state,cap_khz,p95_ms,energy_mj,power_w")
        throw std::runtime_error("Invalid profile CSV header");
    while (std::getline(f,line)) {
        if (line.empty()) continue;
        std::replace(line.begin(),line.end(),',',' ');
        std::istringstream s(line);
        std::string d,st,lat,e,p; long cap;
        if (!(s>>d>>st>>cap>>lat>>e>>p) ||
            (d!="cpu" && d!="gpu") || (st!="active" && st!="suspended" && st!="unknown"))
            throw std::runtime_error("Invalid profile row");
        std::string workload = "pool";
        if (tagged && !(s >> workload)) throw std::runtime_error("Missing workload in profile");
        out[workload].push_back({d=="cpu"?pm::Device::cpu:pm::Device::gpu,
            st=="active"?pm::State::active:st=="suspended"?pm::State::suspended:pm::State::unknown,
            cap,std::stod(lat),std::stod(e),std::stod(p)});
    }
    return out;
}
int main(int argc, char** argv) try {
    std::map<std::string,std::string> args;
    if ((argc-1)%2) throw std::runtime_error("Use --key value pairs; see README");
    for(int i=1;i<argc;i+=2) args[argv[i]]=argv[i+1];
    auto get=[&](const std::string& k,const std::string& d){auto i=args.find(k); return i==args.end()?d:i->second;};
    auto integer=[&](const std::string& k,int d){return std::stoi(get(k,std::to_string(d)));};
    auto real=[&](const std::string& k,double d){return std::stod(get(k,std::to_string(d)));};
    const std::map<std::string,pm::Mode> modes={{"cpu",pm::Mode::cpu},{"gpu",pm::Mode::gpu},{"rr",pm::Mode::rr},{"eft",pm::Mode::eft},{"energy",pm::Mode::energy},{"adaptive",pm::Mode::adaptive}};
    const bool lookahead=get("--mode","cpu")=="lookahead";
    const bool dvfs=lookahead || get("--mode","cpu")=="dvfs";
    auto mode=dvfs?pm::Mode::cpu:modes.at(get("--mode","cpu"));
    bool gpu=mode!=pm::Mode::cpu && integer("--gpu",1)!=0;
    if(mode==pm::Mode::gpu && !gpu) throw std::runtime_error("GPU mode requires GPU");
    std::vector<std::string> workloads;
    std::istringstream workload_list(get("--workload","pool"));
    for (std::string model; std::getline(workload_list,model,',');) {
        if (model!="pool" && model!="small" && model!="medium" && model!="large")
            throw std::runtime_error("Workload must be pool, small, medium or large");
        workloads.push_back(model);
    }
    if (workloads.empty()) throw std::runtime_error("Empty workload list");
    const bool pool=workloads.front()=="pool";
    for (const auto& model : workloads)
        if ((model=="pool")!=pool) throw std::runtime_error("Cannot mix grayscale pool and RGB models");
    const int channels=pool?1:3;
    const int w=integer("--width",pool?320:224), h=integer("--height",pool?240:224), jobs=integer("--jobs",100);
    if (!pool && (w!=224 || h!=224)) throw std::runtime_error("MobileNetV4 exports require 224x224 RGB");
    const bool verify=integer("--verify",0)!=0;
    if (verify && (mode!=pm::Mode::gpu || workloads.size()!=1))
        throw std::runtime_error("Verification requires --mode gpu and a single workload");
    const int period=integer("--period-ms",100), burst=integer("--burst",10), gap=integer("--gap-ms",0);
    const int threads=integer("--threads",2), index=integer("--gpu-index",0);
    const double deadline=real("--deadline-ms",100), budget=real("--power-w",0);
    const auto path=get("--gpu-device",""), zone=get("--thermal-zone","/sys/class/thermal/thermal_zone0");
    const auto model=get("--param","models/pool.param"), bin=get("--bin","models/pool.bin");
    const auto model_dir=get("--model-dir","models/mobilenetv4");
    if (!pool && (args.count("--param") || args.count("--bin")))
        throw std::runtime_error("Use --model-dir for MobileNetV4");
    const bool thermal=integer("--thermal",0)!=0;
    const double low=real("--thermal-low",65), high=real("--thermal-high",70);
    const int dwell=integer("--thermal-dwell-ms",1000);
    if(w<=0||h<=0||w>4096||h>4096||jobs<=0||period<=0||burst<=0||gap<0||threads<=0||
       !std::isfinite(deadline)||deadline<=0||!std::isfinite(budget)||budget<0||
       !std::isfinite(low)||!std::isfinite(high)||low>=high||high>75||dwell<100)
        throw std::runtime_error("Invalid workload or thermal settings");
    if(budget>0 && mode!=pm::Mode::adaptive) throw std::runtime_error("Power budget requires adaptive mode");
    std::vector<double> deadlines;
    for(const auto& s:pm::split_csv(get("--deadlines-ms",std::to_string(deadline)))) {
        double d=pm::number(s);
        if(d<=0) throw std::runtime_error("Invalid per-job deadline");
        deadlines.push_back(d);
    }
    if(deadlines.empty() || (deadlines.size()!=1 && deadlines.size()!=workloads.size()))
        throw std::runtime_error("Provide one deadline or one per workload entry");
    const double margin=real("--dvfs-margin-ms",3), saving=real("--dvfs-min-saving",.05);
    const double switch_dwell=real("--dvfs-dwell-ms",250);
    const int switch_timeout=integer("--dvfs-timeout-ms",100);
    const bool ignore_switch=integer("--dvfs-ignore-transition",0)!=0;
    const int lead_ms=integer("--dvfs-lead-ms",150), startup_ms=integer("--startup-lead-ms",lookahead?200:0);
    if((lookahead && (lead_ms<0||lead_ms>period))||startup_ms<0||startup_ms>1000)
        throw std::runtime_error("Invalid look-ahead or startup lead");
    if(dvfs && (pool || gap!=0 || !std::isfinite(margin)||margin<0 ||
       !std::isfinite(saving)||saving<0||saving>=1 || !std::isfinite(switch_dwell)||switch_dwell<0 ||
       switch_timeout<10||switch_timeout>1000))
        throw std::runtime_error("Invalid DVFS configuration (MobileNet, fixed period, no extra gap)");
    auto costs=profile(get("--profile",""));
    if(mode==pm::Mode::eft||mode==pm::Mode::energy||mode==pm::Mode::adaptive)
        for (const auto& workload : workloads)
            if (!costs.count(workload)) throw std::runtime_error("Missing calibrated profile for "+workload);
    pm::CpuCap cap(get("--cpufreq-policy","/sys/devices/system/cpu/cpufreq/policy0"));
    auto freqs=cap.frequencies();
    if(thermal && (freqs.empty() || !std::isfinite(pm::temperature(zone))))
        throw std::runtime_error("Thermal mode requires readable zone and frequency table");
    pm::CpuCosts cpu_costs;
    pm::SwitchCosts switches;
    std::unique_ptr<pm::DvfsActuator> actuator;
    std::vector<long> calibrated;
    long thermal_ceiling=0, expected_cap=0;
    if(dvfs) {
        cpu_costs=pm::load_cpu_costs(get("--cpu-costs",""),threads,period);
        switches=pm::load_switch_costs(get("--transition-costs",""),integer("--dvfs-use-max",0)!=0);
        actuator=std::make_unique<pm::DvfsActuator>(get("--cpufreq-policy","/sys/devices/system/cpu/cpufreq/policy0"));
        for(const auto& workload:workloads) {
            const auto& rows=cpu_costs.at(workload);
            std::vector<long> fs;
            for(const auto& c:rows) {
                if(!std::binary_search(freqs.begin(),freqs.end(),c.khz))
                    throw std::runtime_error("Profile frequency outside current policy");
                fs.push_back(c.khz);
            }
            std::sort(fs.begin(),fs.end());
            if(calibrated.empty()) calibrated=fs;
            else if(calibrated!=fs) throw std::runtime_error("Models need identical calibrated frequency sets");
        }
        if(calibrated.empty() || !std::binary_search(calibrated.begin(),calibrated.end(),actuator->initial()))
            throw std::runtime_error("Initial cap needs a calibrated cost");
        for(long a:calibrated) for(long b:calibrated)
            if(!pm::switch_cost(switches,a,b)) throw std::runtime_error("Incomplete directed transition matrix");
        thermal_ceiling=calibrated.back();
        expected_cap=actuator->initial();
        actuator->set_ready(expected_cap,switch_timeout);
    }
    pm::VulkanContext vk(gpu,index);
    struct Engines { std::unique_ptr<pm::Backend> cpu, accelerator; };
    std::map<std::string, Engines> engines;
    for (const auto& workload : workloads) {
        if (engines.count(workload)) continue;
        auto& e = engines[workload];
        const auto param_path = pool ? model : model_dir+"/"+workload+".param";
        const auto bin_path = pool ? bin : model_dir+"/"+workload+".bin";
        if(mode!=pm::Mode::gpu || verify) e.cpu=std::make_unique<pm::Backend>(param_path,bin_path,false,threads,index);
        if(gpu) e.accelerator=std::make_unique<pm::Backend>(param_path,bin_path,true,threads,index);
    }
    std::vector<unsigned char> frame(static_cast<size_t>(w)*h*channels);
    if (verify) {
        for (size_t k=0;k<frame.size();++k) frame[k]=static_cast<unsigned char>((k*17u)%256u);
        auto& e=engines.at(workloads.front());
        std::vector<float> a,b;
        e.cpu->run(frame,w,h,channels,&a);
        e.accelerator->run(frame,w,h,channels,&b);
        if (a.size()!=b.size() || a.empty() || (!pool && a.size()!=1000))
            throw std::runtime_error("CPU/GPU output shape mismatch");
        double max_abs=0.;
        size_t violations=0;
        for (size_t i=0;i<a.size();++i) {
            double diff=std::abs(static_cast<double>(a[i])-b[i]);
            max_abs=std::max(max_abs,diff);
            if (diff>0.001+0.001*std::abs(a[i])) ++violations;
        }
        if (args.count("--reference")) {
            std::ifstream ref(get("--reference",""));
            if (!ref) throw std::runtime_error("Cannot open reference logits");
            double x; size_t i=0, errors=0;
            while (ref>>x) {
                if (i>=a.size() || !std::isfinite(x)) throw std::runtime_error("Reference shape/value mismatch");
                if (std::abs(x-a[i])>0.001+0.001*std::abs(x)) ++errors;
                ++i;
            }
            if (!ref.eof() || i!=a.size() || errors) throw std::runtime_error("PyTorch/CPU logits mismatch");
            std::cerr<<"PyTorch/CPU parity passed ("<<i<<" values)\n";
        }
        std::cout<<"values="<<a.size()<<" max_abs_error="<<max_abs<<" violations="<<violations<<'\n';
        return violations?1:0;
    }
    std::ifstream raw;
    auto rawpath=get("--raw","");
    if(!rawpath.empty()) { raw.open(rawpath,std::ios::binary); if(!raw) throw std::runtime_error("Cannot open raw video"); }
    std::signal(SIGINT,on_signal); std::signal(SIGTERM,on_signal); std::signal(SIGHUP,on_signal);
    auto start=Clock::now()+milliseconds(startup_ms), last_thermal=Clock::now()-milliseconds(dwell);
    auto last_switch=Clock::now()-duration_cast<Clock::duration>(duration<double,std::milli>(switch_dwell));
    auto sleep_to=[&](Clock::time_point when) {
        while(!stop && Clock::now()<when)
            std::this_thread::sleep_until(std::min(when,Clock::now()+milliseconds(50)));
    };
    auto check_thermal=[&](double temp) {
        if(!std::isfinite(temp)||temp>=75.) throw std::runtime_error("DVFS thermal guard");
        auto now=Clock::now();
        if(thermal && now-last_thermal>=milliseconds(dwell)) {
            thermal_ceiling=pm::thermal_step(thermal_ceiling,temp,low,high,calibrated);
            last_thermal=now;
        }
    };
    std::cout<<"job,release_ms,dispatch_ms,finish_ms,backend,gpu_state,cap_khz,temp_c,service_ms,response_ms,deadline_miss,status,checksum,workload,width,height,threads,deadline_ms,from_khz,target_khz,reported_freq_khz,transition_ms,predicted_ms,predicted_feasible,decision,thermal_ceiling_khz,queue_ms,transition_write_ms,transition_cap_seen_ms,transition_freq_seen_ms,transition_late,transition_start_ms,transition_ready_ms,transition_on_path_ms,pre_release_ready\n";
    for(int i=0;i<jobs&&!stop;++i) {
        auto release=start+milliseconds(static_cast<long long>(i)*period+static_cast<long long>(i/burst)*gap);
        const auto& workload=workloads[i%workloads.size()];
        const double job_deadline=deadlines[i%deadlines.size()];
        pm::ReadyResult timing;
        double switch_start_ms=0,switch_ready_ms=0,on_path_ms=0;
        int pre_ready=-1;
        long planned_from=0, planned_target=0;
        double plan_prediction=0;
        int plan_feasible=-1;
        const char* plan_reason="fixed";
        auto apply_switch=[&](long target) {
            auto before=Clock::now();
            timing=actuator->set_ready(target,switch_timeout);
            auto after=Clock::now();
            switch_start_ms=ms(before-start);
            switch_ready_ms=ms(after-start);
            on_path_ms=std::max(0.,ms(after-std::max(before,release)));
            pre_ready=after<=release;
            last_switch=after;
            expected_cap=target;
        };
        if(lookahead) {
            actuator->validate();
            planned_from=actuator->cap();
            if(planned_from!=expected_cap || actuator->freq()!=planned_from)
                throw std::runtime_error("CPU cap/frequency drift before look-ahead");
            check_thermal(pm::temperature(zone));
            auto now=Clock::now();
            const auto late_start=std::max(now,release-milliseconds(lead_ms));
            const double hidden=std::max(0.,ms(release-late_start));
            auto decision=pm::choose_dvfs(cpu_costs.at(workload),switches,planned_from,thermal_ceiling,
                job_deadline-std::max(0.,ms(now-release)),margin,ms(late_start-last_switch),
                switch_dwell,saving,ignore_switch,hidden);
            planned_target=decision.khz;
            plan_prediction=decision.predicted_ms;
            plan_feasible=decision.feasible;
            plan_reason=decision.reason;
            if(planned_target!=planned_from) {
                auto when=planned_target>planned_from?late_start:now;
                bool stay_feasible=false;
                for(const auto& c:cpu_costs.at(workload))
                    if(c.khz==planned_from && c.khz<=thermal_ceiling &&
                       c.service_ms+margin<=job_deadline-std::max(0.,ms(now-release)))
                        stay_feasible=true;
                if(stay_feasible) {
                    auto dwell_end=last_switch+duration_cast<Clock::duration>(duration<double,std::milli>(switch_dwell));
                    when=std::max(when,dwell_end);
                }
                sleep_to(when);
                if(stop) break;
                check_thermal(pm::temperature(zone));
                if(planned_target>thermal_ceiling) {
                    auto fallback=pm::choose_dvfs(cpu_costs.at(workload),switches,planned_from,thermal_ceiling,
                        job_deadline-std::max(0.,ms(Clock::now()-release)),margin,
                        ms(Clock::now()-last_switch),switch_dwell,saving,ignore_switch,
                        std::max(0.,ms(release-Clock::now())));
                    planned_target=fallback.khz;
                    plan_prediction=fallback.predicted_ms;
                    plan_feasible=fallback.feasible;
                    plan_reason="thermal_replan";
                }
                if(planned_target!=planned_from) apply_switch(planned_target);
            }
        }
        sleep_to(release);
        if(stop) break;
        if(raw.is_open()) {
            if(!raw.read(reinterpret_cast<char*>(frame.data()),frame.size())) {
                raw.clear(); raw.seekg(0);
                if(!raw.read(reinterpret_cast<char*>(frame.data()),frame.size())) throw std::runtime_error("Raw video has no complete frame");
            }
        } else for(size_t k=0;k<frame.size();++k) frame[k]=static_cast<unsigned char>((k*17u+static_cast<unsigned>(i)*31u)%256u);
        auto now=Clock::now();
        double temp=pm::temperature(zone);
        if(thermal && !dvfs && now-last_thermal>=milliseconds(dwell)) {
            if(!std::isfinite(temp)) throw std::runtime_error("Temperature unavailable");
            cap.set(pm::thermal_step(cap.current(),temp,low,high,freqs)); last_thermal=now;
        }
        auto state=pm::runtime_state(path);
        long khz=cap.current();
        if(khz<0) khz=0;
        auto dispatch=Clock::now();
        auto& e=engines.at(workload);
        const long from=lookahead?planned_from:khz;
        double transition_ms=0, predicted_ms=0;
        int predicted_feasible=-1;
        const char* reason="fixed";
        long target=khz, observed_freq=pm::read_long(get("--cpufreq-policy","/sys/devices/system/cpu/cpufreq/policy0")+"/scaling_cur_freq");
        if(lookahead) {
            actuator->validate();
            if(!std::isfinite(temp)||temp>=75.) throw std::runtime_error("DVFS thermal guard");
            target=planned_target;
            predicted_ms=plan_prediction;
            predicted_feasible=plan_feasible;
            reason=plan_reason;
            transition_ms=timing.elapsed_ms;
            khz=actuator->cap();
            observed_freq=actuator->freq();
            if(khz!=target || observed_freq!=target || khz!=expected_cap)
                throw std::runtime_error("Look-ahead target not observed at dispatch");
        } else if(dvfs) {
            actuator->validate();
            if(khz!=expected_cap || actuator->freq()!=khz)
                throw std::runtime_error("CPU cap/frequency drift; cannot apply calibrated transition model");
            check_thermal(temp);
            auto decision=pm::choose_dvfs(cpu_costs.at(workload),switches,khz,thermal_ceiling,
                job_deadline-ms(Clock::now()-release),margin,ms(Clock::now()-last_switch),
                switch_dwell,saving,ignore_switch);
            target=decision.khz;
            predicted_ms=decision.predicted_ms;
            predicted_feasible=decision.feasible;
            reason=decision.reason;
            if(target!=khz) {
                apply_switch(target);
                transition_ms=timing.elapsed_ms;
            }
            khz=actuator->cap();
            observed_freq=actuator->freq();
            if(khz!=target || observed_freq!=target)
                throw std::runtime_error("DVFS target not observed at dispatch");
        }
        auto pick=pm::select(mode,state,khz,job_deadline-ms(Clock::now()-release),budget,gpu,costs[workload],i);
        double checksum=0, service=0;
        const char* status="rejected";
        if(pick.device) {
            auto& backend=*pick.device==pm::Device::cpu?e.cpu:e.accelerator;
            if(!backend) throw std::runtime_error("Backend unavailable");
            auto before=Clock::now(); checksum=backend->run(frame,w,h,channels); service=ms(Clock::now()-before); status="ok";
        }
        auto finish=Clock::now();
        std::cout<<std::fixed<<std::setprecision(6)<<i<<','<<ms(release-start)<<','<<ms(dispatch-start)<<','<<ms(finish-start)<<','
            <<(pick.device?pm::name(*pick.device):"none")<<','<<pm::name(state)<<','<<khz<<','<<temp<<','<<service<<','
            <<ms(finish-release)<<','<<(!pick.device||ms(finish-release)>job_deadline)<<','<<status<<','<<checksum<<','<<workload<<','<<w<<','<<h<<','<<threads<<','
            <<job_deadline<<','<<from<<','<<target<<','<<observed_freq<<','<<transition_ms<<','
            <<predicted_ms<<','<<predicted_feasible<<','<<reason<<','<<thermal_ceiling<<','<<ms(dispatch-release)<<','
            <<timing.write_ms<<','<<timing.cap_seen_ms<<','<<timing.freq_seen_ms<<','<<timing.late<<','
            <<switch_start_ms<<','<<switch_ready_ms<<','<<on_path_ms<<','<<pre_ready<<'\n'<<std::flush;
    }
    if(!stop && integer("--tail-idle",0)!=0) {
        const auto horizon=start+milliseconds(static_cast<long long>(jobs)*period+
            static_cast<long long>((jobs-1)/burst)*gap);
        while(!stop && Clock::now()<horizon)
            std::this_thread::sleep_until(std::min(horizon,Clock::now()+milliseconds(50)));
    }
    if(actuator) actuator->restore();
    return 0;
} catch(const std::exception& e) { std::cerr<<"pm_runner: "<<e.what()<<'\n'; return 1; }
