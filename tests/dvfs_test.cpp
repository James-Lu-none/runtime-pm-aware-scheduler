#include "pm/dvfs.hpp"
#include "pm/dvfs_sysfs.hpp"
#include <iostream>
#include <cstdlib>
using namespace pm;
static void check(bool ok,const char* label) {
    if(!ok) {std::cerr<<label<<'\n';std::exit(1);}
}
int main() {
    const long a=1500000,b=2000000,c=2400000;
    SwitchCosts sw={{{a,b},5.044},{{a,c},5.063},{{b,a},3.789},{{b,c},7.574},{{c,a},11.712},{{c,b},5.615}};
    std::vector<CpuCost> small={{a,13.328,265.947},{b,11.122,406.802},{c,10.426,525.665}};
    std::vector<CpuCost> large={{a,78.096,393.248},{b,64.137,620.266},{c,57.864,739.883}};
    auto pick=[&](auto& costs,long current,long ceil,double slack,double age=1000.) {
        return choose_dvfs(costs,sw,current,ceil,slack,3,age,250,.05);
    };
    check(pick(small,a,c,18).khz==a,"small avoids costly upscale");
    check(pick(large,a,c,68).khz==c,"tight large requires 2.4");
    check(pick(large,a,c,74).khz==b,"medium deadline permits 2.0");
    check(pick(large,c,c,100).khz==a,"loose large downscales");
    check(pick(large,c,c,100,10).khz==c,"dwell retains feasible current");
    check(pick(large,a,c,68,10).khz==c,"deadline overrides dwell");
    check(pick(large,c,b,100,10).khz==a,"thermal overrides dwell");
    auto x=pick(large,a,b,20);
    check(!x.feasible && x.khz==b,"best effort respects thermal ceiling");
    auto y=choose_dvfs(large,sw,a,c,68,3,1000,250,.05,true);
    check(y.khz==b,"transition-blind ablation");
    auto z=choose_dvfs(large,{},a,c,20,3,1000,250,.05);
    check(z.khz==a&&!z.feasible,"unknown transitions never zero");
    check(choose_dvfs(small,sw,c,c,18,3,1000,250,.05,false,150).khz==a,
          "pre-release downscale makes small feasible at low frequency");
    check(choose_dvfs(large,sw,a,c,68,3,1000,250,.05,false,150).khz==b,
          "pre-release upscale permits medium frequency");
    check(choose_dvfs(large,sw,a,c,68,3,1000,250,.05,false,1).khz==c,
          "short lead retains residual transition penalty");
    check(choose_dvfs(large,sw,a,b,68,3,1000,250,.05,false,150).khz==b,
          "look-ahead respects ceiling");
    check(!ready_late(100,100)&&ready_late(126.739,100),"late readback not on-time success");
}
