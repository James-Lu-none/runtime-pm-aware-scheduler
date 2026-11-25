#include <vulkan/vulkan.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>
#if defined(__aarch64__)
#include <arm_neon.h>
#endif

using Clock = std::chrono::steady_clock;
static double ms(Clock::duration d) {
    return std::chrono::duration<double,std::milli>(d).count();
}
static void check(VkResult r, const char* what) {
    if (r != VK_SUCCESS) throw std::runtime_error(std::string(what)+": "+std::to_string(r));
}
static void cpu_run(const float* in, float* out, uint32_t n, uint32_t rounds, int threads) {
    constexpr float a=.99991f, b=.00013f;
    #pragma omp parallel for schedule(static) num_threads(threads)
    for (int64_t i=0; i<static_cast<int64_t>(n); i+=16) {
#if defined(__aarch64__)
        const auto va=vdupq_n_f32(a), vb=vdupq_n_f32(b);
        auto x0=vld1q_f32(in+i), x1=vld1q_f32(in+i+4);
        auto x2=vld1q_f32(in+i+8), x3=vld1q_f32(in+i+12);
        for (uint32_t j=0; j<rounds; ++j) {
            x0=vfmaq_f32(vb,x0,va); x1=vfmaq_f32(vb,x1,va);
            x2=vfmaq_f32(vb,x2,va); x3=vfmaq_f32(vb,x3,va);
        }
        vst1q_f32(out+i,x0); vst1q_f32(out+i+4,x1);
        vst1q_f32(out+i+8,x2); vst1q_f32(out+i+12,x3);
#else
        float x[16];
        for (int k=0;k<16;++k) x[k]=in[i+k];
        for (uint32_t j=0;j<rounds;++j)
            for (auto& v:x) v=std::fma(v,a,b);
        for (int k=0;k<16;++k) out[i+k]=x[k];
#endif
    }
}
struct Buffer {
    VkBuffer buffer=VK_NULL_HANDLE;
    VkDeviceMemory memory=VK_NULL_HANDLE;
    float* mapped=nullptr;
};
class Gpu {
    VkInstance instance=VK_NULL_HANDLE;
    VkPhysicalDevice physical=VK_NULL_HANDLE;
    VkDevice device=VK_NULL_HANDLE;
    VkQueue queue=VK_NULL_HANDLE;
    VkDescriptorSetLayout layout=VK_NULL_HANDLE;
    VkPipelineLayout pipeline_layout=VK_NULL_HANDLE;
    VkShaderModule shader=VK_NULL_HANDLE;
    VkPipeline pipeline=VK_NULL_HANDLE;
    VkDescriptorPool descriptors=VK_NULL_HANDLE;
    VkCommandPool commands=VK_NULL_HANDLE;
    VkCommandBuffer cmd=VK_NULL_HANDLE;
    VkFence fence=VK_NULL_HANDLE;
    Buffer input,output;
    void allocate(Buffer& b, VkDeviceSize size) {
        VkBufferCreateInfo ci{};
        ci.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        ci.size=size; ci.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
        ci.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
        check(vkCreateBuffer(device,&ci,nullptr,&b.buffer),"create buffer");
        VkMemoryRequirements req{};
        vkGetBufferMemoryRequirements(device,b.buffer,&req);
        VkPhysicalDeviceMemoryProperties mem{};
        vkGetPhysicalDeviceMemoryProperties(physical,&mem);
        uint32_t index=mem.memoryTypeCount;
        const VkMemoryPropertyFlags wanted=VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        for (uint32_t i=0;i<mem.memoryTypeCount;++i)
            if ((req.memoryTypeBits&(1u<<i)) && (mem.memoryTypes[i].propertyFlags&wanted)==wanted) {
                index=i;
                if (mem.memoryTypes[i].propertyFlags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) break;
            }
        if(index==mem.memoryTypeCount) throw std::runtime_error("No coherent host-visible storage memory");
        VkMemoryAllocateInfo ai{};
        ai.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
        ai.allocationSize=req.size; ai.memoryTypeIndex=index;
        check(vkAllocateMemory(device,&ai,nullptr,&b.memory),"allocate memory");
        check(vkBindBufferMemory(device,b.buffer,b.memory,0),"bind buffer");
        void* ptr=nullptr;
        check(vkMapMemory(device,b.memory,0,VK_WHOLE_SIZE,0,&ptr),"map buffer");
        b.mapped=static_cast<float*>(ptr);
    }
    void release(Buffer& b) {
        if(b.mapped) vkUnmapMemory(device,b.memory);
        if(b.buffer) vkDestroyBuffer(device,b.buffer,nullptr);
        if(b.memory) vkFreeMemory(device,b.memory,nullptr);
    }
public:
    Gpu(const std::vector<float>& data, uint32_t rounds, int index) {
        VkApplicationInfo app{};
        app.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO; app.apiVersion=VK_API_VERSION_1_0;
        VkInstanceCreateInfo ic{};
        ic.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO; ic.pApplicationInfo=&app;
        check(vkCreateInstance(&ic,nullptr,&instance),"create instance");
        uint32_t count=0;
        check(vkEnumeratePhysicalDevices(instance,&count,nullptr),"enumerate GPUs");
        std::vector<VkPhysicalDevice> devices(count);
        check(vkEnumeratePhysicalDevices(instance,&count,devices.data()),"enumerate GPUs");
        if(index<0 || static_cast<uint32_t>(index)>=count) throw std::runtime_error("Invalid GPU index");
        physical=devices[index];
        VkPhysicalDeviceProperties props{};
        vkGetPhysicalDeviceProperties(physical,&props);
        if(props.deviceType!=VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU && props.deviceType!=VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU)
            throw std::runtime_error("Refusing non-hardware GPU");
        std::cerr<<"Vulkan device: "<<props.deviceName<<'\n';
        const uint32_t blocks=static_cast<uint32_t>(data.size()/16);
        const uint32_t groups=(blocks+63)/64;
        const VkDeviceSize bytes=data.size()*sizeof(float);
        if(groups>props.limits.maxComputeWorkGroupCount[0] ||
           props.limits.maxComputeWorkGroupSize[0]<64 ||
           props.limits.maxComputeWorkGroupInvocations<64 ||
           bytes>props.limits.maxStorageBufferRange)
            throw std::runtime_error("Workload exceeds Vulkan limits");
        uint32_t nq=0;
        vkGetPhysicalDeviceQueueFamilyProperties(physical,&nq,nullptr);
        std::vector<VkQueueFamilyProperties> families(nq);
        vkGetPhysicalDeviceQueueFamilyProperties(physical,&nq,families.data());
        uint32_t family=0;
        while(family<nq && (!(families[family].queueFlags&VK_QUEUE_COMPUTE_BIT)||!families[family].queueCount)) ++family;
        if(family==nq) throw std::runtime_error("No compute queue");
        float priority=1.f;
        VkDeviceQueueCreateInfo qi{};
        qi.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
        qi.queueFamilyIndex=family; qi.queueCount=1; qi.pQueuePriorities=&priority;
        VkDeviceCreateInfo di{};
        di.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO; di.queueCreateInfoCount=1; di.pQueueCreateInfos=&qi;
        check(vkCreateDevice(physical,&di,nullptr,&device),"create device");
        vkGetDeviceQueue(device,family,0,&queue);
        allocate(input,bytes); allocate(output,bytes);
        std::memcpy(input.mapped,data.data(),static_cast<size_t>(bytes));
        VkDescriptorSetLayoutBinding bindings[2]{};
        for(uint32_t i=0;i<2;++i) {
            bindings[i].binding=i; bindings[i].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            bindings[i].descriptorCount=1; bindings[i].stageFlags=VK_SHADER_STAGE_COMPUTE_BIT;
        }
        VkDescriptorSetLayoutCreateInfo li{};
        li.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
        li.bindingCount=2; li.pBindings=bindings;
        check(vkCreateDescriptorSetLayout(device,&li,nullptr,&layout),"descriptor layout");
        VkPushConstantRange push{};
        push.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT; push.size=8;
        VkPipelineLayoutCreateInfo pli{};
        pli.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
        pli.setLayoutCount=1; pli.pSetLayouts=&layout;
        pli.pushConstantRangeCount=1; pli.pPushConstantRanges=&push;
        check(vkCreatePipelineLayout(device,&pli,nullptr,&pipeline_layout),"pipeline layout");
        std::ifstream file(PM_SHADER_PATH,std::ios::binary|std::ios::ate);
        if(!file) throw std::runtime_error("Cannot read SPIR-V shader");
        auto size=file.tellg();
        if(size<=0 || static_cast<size_t>(size)%4) throw std::runtime_error("Invalid SPIR-V size");
        std::vector<uint32_t> code(static_cast<size_t>(size)/4);
        file.seekg(0);
        if(!file.read(reinterpret_cast<char*>(code.data()),size)) throw std::runtime_error("SPIR-V read failed");
        VkShaderModuleCreateInfo si{};
        si.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
        si.codeSize=code.size()*4; si.pCode=code.data();
        check(vkCreateShaderModule(device,&si,nullptr,&shader),"shader module");
        VkComputePipelineCreateInfo pi{};
        pi.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
        pi.stage.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        pi.stage.stage=VK_SHADER_STAGE_COMPUTE_BIT; pi.stage.module=shader; pi.stage.pName="main";
        pi.layout=pipeline_layout;
        check(vkCreateComputePipelines(device,VK_NULL_HANDLE,1,&pi,nullptr,&pipeline),"compute pipeline");
        VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,2};
        VkDescriptorPoolCreateInfo dpi{};
        dpi.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
        dpi.maxSets=1; dpi.poolSizeCount=1; dpi.pPoolSizes=&ps;
        check(vkCreateDescriptorPool(device,&dpi,nullptr,&descriptors),"descriptor pool");
        VkDescriptorSetAllocateInfo dsa{};
        dsa.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        dsa.descriptorPool=descriptors; dsa.descriptorSetCount=1; dsa.pSetLayouts=&layout;
        VkDescriptorSet set=VK_NULL_HANDLE;
        check(vkAllocateDescriptorSets(device,&dsa,&set),"descriptor set");
        VkDescriptorBufferInfo db[2]={{input.buffer,0,bytes},{output.buffer,0,bytes}};
        VkWriteDescriptorSet writes[2]{};
        for(uint32_t i=0;i<2;++i) {
            writes[i].sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET; writes[i].dstSet=set;
            writes[i].dstBinding=i; writes[i].descriptorCount=1;
            writes[i].descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER; writes[i].pBufferInfo=&db[i];
        }
        vkUpdateDescriptorSets(device,2,writes,0,nullptr);
        VkCommandPoolCreateInfo cp{};
        cp.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO; cp.queueFamilyIndex=family;
        check(vkCreateCommandPool(device,&cp,nullptr,&commands),"command pool");
        VkCommandBufferAllocateInfo cb{};
        cb.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
        cb.commandPool=commands; cb.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY; cb.commandBufferCount=1;
        check(vkAllocateCommandBuffers(device,&cb,&cmd),"command buffer");
        VkCommandBufferBeginInfo begin{};
        begin.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        check(vkBeginCommandBuffer(cmd,&begin),"begin command");
        vkCmdBindPipeline(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);
        vkCmdBindDescriptorSets(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline_layout,0,1,&set,0,nullptr);
        const uint32_t constants[]={blocks,rounds};
        vkCmdPushConstants(cmd,pipeline_layout,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(constants),constants);
        vkCmdDispatch(cmd,groups,1,1);
        VkMemoryBarrier barrier{};
        barrier.sType=VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        barrier.srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT; barrier.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
        vkCmdPipelineBarrier(cmd,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&barrier,0,nullptr,0,nullptr);
        check(vkEndCommandBuffer(cmd),"end command");
        VkFenceCreateInfo fi{}; fi.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        check(vkCreateFence(device,&fi,nullptr,&fence),"create fence");
    }
    Gpu(const Gpu&)=delete;
    Gpu& operator=(const Gpu&)=delete;
    ~Gpu() {
        if(device) {
            vkDeviceWaitIdle(device);
            if(fence) vkDestroyFence(device,fence,nullptr);
            if(commands) vkDestroyCommandPool(device,commands,nullptr);
            if(descriptors) vkDestroyDescriptorPool(device,descriptors,nullptr);
            if(pipeline) vkDestroyPipeline(device,pipeline,nullptr);
            if(shader) vkDestroyShaderModule(device,shader,nullptr);
            if(pipeline_layout) vkDestroyPipelineLayout(device,pipeline_layout,nullptr);
            if(layout) vkDestroyDescriptorSetLayout(device,layout,nullptr);
            release(input); release(output);
            vkDestroyDevice(device,nullptr);
        }
        if(instance) vkDestroyInstance(instance,nullptr);
    }
    const float* run() {
        check(vkResetFences(device,1,&fence),"reset fence");
        VkSubmitInfo s{};
        s.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO; s.commandBufferCount=1; s.pCommandBuffers=&cmd;
        check(vkQueueSubmit(queue,1,&s,fence),"submit");
        check(vkWaitForFences(device,1,&fence,VK_TRUE,UINT64_MAX),"wait");
        return output.mapped;
    }
};
int main(int argc,char** argv) try {
    std::map<std::string,std::string> args;
    if((argc-1)%2) throw std::runtime_error("Use --key value pairs");
    for(int i=1;i<argc;i+=2) args[argv[i]]=argv[i+1];
    auto get=[&](const std::string& k,const std::string& d) {
        auto it=args.find(k); return it==args.end()?d:it->second;
    };
    auto integer=[&](const std::string& k,int d){return std::stoi(get(k,std::to_string(d)));};
    const auto mode=get("--mode","cpu");
    const int elements=integer("--elements",262144), rounds=integer("--iterations",64);
    const int threads=integer("--threads",2), index=integer("--gpu-index",0);
    const int seconds=integer("--seconds",5), min_jobs=integer("--min-jobs",10);
    if((mode!="cpu" && mode!="gpu" && mode!="verify")||elements<16||elements>16777216||
       elements%16||rounds<1||rounds>16384||threads<1||threads>64||seconds<1||seconds>120||min_jobs<1||min_jobs>1000)
        throw std::runtime_error("Invalid mode, elements (multiple of 16), iterations or timing");
    std::vector<float> input(elements),out(elements);
    for(int i=0;i<elements;++i) input[i]=static_cast<float>((i*17u)%1009u)/1009.f;
    if(mode=="verify") {
        cpu_run(input.data(),out.data(),elements,rounds,threads);
        Gpu gpu(input,rounds,index);
        const float* result=gpu.run();
        double max_abs=0.; size_t errors=0;
        for(int i=0;i<elements;++i) {
            const double diff=std::abs(static_cast<double>(out[i])-result[i]);
            max_abs=std::max(max_abs,diff);
            if(!std::isfinite(result[i])||diff>0.0001+0.0001*std::abs(out[i])) ++errors;
        }
        std::cout<<"values="<<elements<<" max_abs_error="<<max_abs<<" violations="<<errors<<'\n';
        return errors?1:0;
    }
    // Initial allocations and pipeline compilation are excluded from service timing.
    // GPU context is deliberately not constructed for CPU-only measurements.
    auto timed=[&](auto run) {
        for(int i=0;i<3;++i) run();
        const auto start=Clock::now();
        std::cout<<"job,release_ms,dispatch_ms,finish_ms,backend,gpu_state,cap_khz,temp_c,service_ms,response_ms,deadline_miss,status,checksum,iterations,elements,threads\n";
        int job=0;
        do {
            const auto begin=Clock::now();
            const float* result=run();
            const double checksum=static_cast<double>(result[0])+result[elements/2]+result[elements-1];
            if(!std::isfinite(checksum)) throw std::runtime_error("Invalid result");
            const auto finish=Clock::now();
            const auto service=ms(finish-begin);
            std::cout<<std::fixed<<std::setprecision(6)<<job<<','<<ms(begin-start)<<','<<ms(begin-start)
                     <<','<<ms(finish-start)<<','<<mode<<",unknown,0,nan,"<<service<<','<<service
                     <<",0,ok,"<<checksum<<','<<rounds<<','<<elements<<','<<threads<<'\n';
            ++job;
        } while(job<min_jobs || ms(Clock::now()-start)<seconds*1000.);
        std::cout<<std::flush;
    };
    if(mode=="cpu") timed([&]() -> const float* {cpu_run(input.data(),out.data(),elements,rounds,threads); return out.data();});
    else {Gpu gpu(input,rounds,index); timed([&](){return gpu.run();});}
    return 0;
} catch(const std::exception& e) {std::cerr<<"pm_microbench: "<<e.what()<<'\n'; return 1;}
