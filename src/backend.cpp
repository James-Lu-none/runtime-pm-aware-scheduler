#include "pm/backend.hpp"
#include <net.h>
#include <gpu.h>
#include <stdexcept>
#include <cmath>
namespace pm {
struct Backend::Impl { ncnn::Net net; };
VulkanContext::VulkanContext(bool enable, int index) : enabled_(enable) {
#if NCNN_VULKAN
    if (enable) {
        if (ncnn::create_gpu_instance() != 0) throw std::runtime_error("Vulkan initialization failed");
        if (index < 0 || index >= ncnn::get_gpu_count()) {
            ncnn::destroy_gpu_instance();
            throw std::runtime_error("Requested Vulkan device unavailable");
        }
    }
#else
    (void)index;
    if (enable) throw std::runtime_error("ncnn built without Vulkan");
#endif
}
VulkanContext::~VulkanContext() {
#if NCNN_VULKAN
    if (enabled_) ncnn::destroy_gpu_instance();
#endif
}
Backend::Backend(const std::string& param, const std::string& bin,
                 bool gpu, int threads, int gpu_index) : impl_(new Impl) {
    auto& n = impl_->net;
    n.opt.num_threads = threads;
    n.opt.use_vulkan_compute = gpu;
    n.opt.use_fp16_packed = false;
    n.opt.use_fp16_storage = false;
    n.opt.use_fp16_arithmetic = false;
#if NCNN_VULKAN
    if (gpu) n.set_vulkan_device(gpu_index);
#else
    (void)gpu_index;
    if (gpu) throw std::runtime_error("No Vulkan backend");
#endif
    if (n.load_param(param.c_str()) || n.load_model(bin.c_str()))
        throw std::runtime_error("ncnn model load failed");
}
Backend::~Backend() = default;
double Backend::run(const std::vector<unsigned char>& gray, int w, int h) {
    ncnn::Mat in(w, h, 1);
    if (in.empty()) throw std::runtime_error("Input allocation failed");
    float* p = in.channel(0);
    for (size_t i = 0; i < gray.size(); ++i) p[i] = gray[i] / 255.f;
    ncnn::Extractor ex = impl_->net.create_extractor();
    ncnn::Mat out;
    if (ex.input("input", in) || ex.extract("output", out) || out.empty())
        throw std::runtime_error("ncnn inference failed");
    if (out.elembits() != 32) throw std::runtime_error("Expected FP32 output");
    // Read actual output, including GPU-to-CPU completion. Avoid padding in cstep.
    double checksum = 0.;
    for (int c = 0; c < out.c; ++c) {
        const float* q = out.channel(c);
        for (size_t i = 0; i < static_cast<size_t>(out.w) * out.h * out.d * out.elempack; ++i) checksum += q[i];
    }
    if (!std::isfinite(checksum)) throw std::runtime_error("Invalid output");
    return checksum;
}
}
