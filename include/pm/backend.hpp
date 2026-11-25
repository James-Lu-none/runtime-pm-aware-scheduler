#pragma once
#include <memory>
#include <string>
#include <vector>
namespace pm {
class Backend {
    struct Impl;
    std::unique_ptr<Impl> impl_;
public:
    Backend(const std::string& param, const std::string& bin,
            bool gpu, int threads, int gpu_index);
    ~Backend();
    double run(const std::vector<unsigned char>& pixels, int w, int h, int channels = 1,
               std::vector<float>* output = nullptr);
};
class VulkanContext {
public:
    explicit VulkanContext(bool enable, int index);
    ~VulkanContext();
    VulkanContext(const VulkanContext&) = delete;
    VulkanContext& operator=(const VulkanContext&) = delete;
private:
    bool enabled_;
};
}
