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
double Backend::run(const std::vector<unsigned char>& pixels, int w, int h, int channels,
                    std::vector<float>* output) {
    if ((channels != 1 && channels != 3) || pixels.size() != static_cast<size_t>(w)*h*channels)
        throw std::runtime_error("Input shape mismatch");
    ncnn::Mat in(w, h, channels);
    if (in.empty()) throw std::runtime_error("Input allocation failed");
    constexpr float mean[] = {.485f, .456f, .406f};
    constexpr float stddev[] = {.229f, .224f, .225f};
    for (int c = 0; c < channels; ++c) {
        float* p = in.channel(c);
        for (size_t i = 0; i < static_cast<size_t>(w)*h; ++i) {
            const float x = pixels[i*channels+c] / 255.f;
            p[i] = channels == 3 ? (x-mean[c])/stddev[c] : x;
        }
    }
    ncnn::Extractor ex = impl_->net.create_extractor();
    ncnn::Mat out;
    if (ex.input("input", in) || ex.extract("output", out) || out.empty())
        throw std::runtime_error("ncnn inference failed");
    if (out.elembits() != 32 || out.elempack != 1) throw std::runtime_error("Expected unpacked FP32 output");
    if (output) output->clear();
    // Read actual output, including GPU-to-CPU completion. Avoid padding in cstep.
    double checksum = 0.;
    for (int c = 0; c < out.c; ++c) {
        const float* q = out.channel(c);
        for (size_t i = 0; i < static_cast<size_t>(out.w) * out.h * out.d; ++i) {
            if (!std::isfinite(q[i])) throw std::runtime_error("Non-finite output");
            checksum += q[i];
            if (output) output->push_back(q[i]);
        }
    }
    if (!std::isfinite(checksum)) throw std::runtime_error("Invalid output");
    return checksum;
}
}
