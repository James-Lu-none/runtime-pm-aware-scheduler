#include "pm/backend.hpp"
#include "pm/platform.hpp"
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
    auto mode=modes.at(get("--mode","cpu"));
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
    auto costs=profile(get("--profile",""));
    if(mode==pm::Mode::eft||mode==pm::Mode::energy||mode==pm::Mode::adaptive)
        for (const auto& workload : workloads)
            if (!costs.count(workload)) throw std::runtime_error("Missing calibrated profile for "+workload);
    pm::CpuCap cap(get("--cpufreq-policy","/sys/devices/system/cpu/cpufreq/policy0"));
    auto freqs=cap.frequencies();
    if(thermal && (freqs.empty() || !std::isfinite(pm::temperature(zone))))
        throw std::runtime_error("Thermal mode requires readable zone and frequency table");
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
    std::signal(SIGINT,on_signal); std::signal(SIGTERM,on_signal);
    auto start=Clock::now(), last_thermal=start-milliseconds(dwell);
    std::cout<<"job,release_ms,dispatch_ms,finish_ms,backend,gpu_state,cap_khz,temp_c,service_ms,response_ms,deadline_miss,status,checksum,workload,width,height,threads\n";
    for(int i=0;i<jobs&&!stop;++i) {
        auto release=start+milliseconds(static_cast<long long>(i)*period+static_cast<long long>(i/burst)*gap);
        while(!stop && Clock::now()<release) std::this_thread::sleep_until(std::min(release,Clock::now()+milliseconds(50)));
        if(stop) break;
        if(raw.is_open()) {
            if(!raw.read(reinterpret_cast<char*>(frame.data()),frame.size())) {
                raw.clear(); raw.seekg(0);
                if(!raw.read(reinterpret_cast<char*>(frame.data()),frame.size())) throw std::runtime_error("Raw video has no complete frame");
            }
        } else for(size_t k=0;k<frame.size();++k) frame[k]=static_cast<unsigned char>((k*17u+static_cast<unsigned>(i)*31u)%256u);
        auto now=Clock::now();
        double temp=pm::temperature(zone);
        if(thermal && now-last_thermal>=milliseconds(dwell)) {
            if(!std::isfinite(temp)) throw std::runtime_error("Temperature unavailable");
            cap.set(pm::thermal_step(cap.current(),temp,low,high,freqs)); last_thermal=now;
        }
        auto state=pm::runtime_state(path);
        long khz=cap.current();
        if(khz<0) khz=0;
        auto dispatch=Clock::now();
        const auto& workload=workloads[i%workloads.size()];
        auto& e=engines.at(workload);
        auto pick=pm::select(mode,state,khz,deadline-ms(dispatch-release),budget,gpu,costs[workload],i);
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
            <<ms(finish-release)<<','<<(!pick.device||ms(finish-release)>deadline)<<','<<status<<','<<checksum<<','<<workload<<','<<w<<','<<h<<','<<threads<<'\n'<<std::flush;
    }
    return 0;
} catch(const std::exception& e) { std::cerr<<"pm_runner: "<<e.what()<<'\n'; return 1; }
