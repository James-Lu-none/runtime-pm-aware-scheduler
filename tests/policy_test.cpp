#include "pm/policy.hpp"
#include <iostream>
#include <cstdlib>
using namespace pm;
static void check(bool ok) { if(!ok) { std::cerr<<"policy test failed\n"; std::exit(1); } }
int main() {
    std::vector<Cost> c={{Device::cpu,State::active,1800000,10,5,1},
                        {Device::gpu,State::active,1800000,4,2,2},
                        {Device::gpu,State::suspended,1800000,15,8,3}};
    check(select(Mode::adaptive,State::active,1800000,20,0,true,c,0).device==Device::gpu);
    check(select(Mode::adaptive,State::suspended,1800000,20,0,true,c,0).device==Device::cpu);
    check(select(Mode::adaptive,State::unknown,1800000,20,0,true,c,0).device==Device::cpu);
    check(select(Mode::adaptive,State::active,1800000,20,1.5,true,c,0).device==Device::cpu);
    check(!select(Mode::adaptive,State::active,1800000,3,0,true,c,0).device);
    check(!select(Mode::adaptive,State::active,0,20,0,true,c,0).device);
    check(select(Mode::eft,State::suspended,1800000,20,0,true,c,0).device==Device::gpu);
    check(thermal_step(1800,72,65,70,{1000,1500,1800,2400})==1500);
    check(thermal_step(1800,63,65,70,{1000,1500,1800,2400})==2400);
    check(thermal_step(1800,67,65,70,{1000,1500,1800,2400})==1800);
}
