// Luma's DCP table evaluator. Keep the operation order and intermediate
// precision of the NumPy reference. Compile with /fp:strict, without FMA.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>

#define API extern "C" __declspec(dllexport)

API int luma_dcp_version() { return 1; }

template<typename T>
static T plane(const T* table, size_t vi, size_t h0, size_t h1,
               size_t s0, size_t s1, size_t hd, size_t sd,
               float hf, float sf, size_t channel) {
    const size_t base = vi * hd * sd * 3;
    const T left = table[base + (h0 * sd + s0) * 3 + channel] * (1.f - sf)
                 + table[base + (h0 * sd + s1) * 3 + channel] * sf;
    const T right = table[base + (h1 * sd + s0) * 3 + channel] * (1.f - sf)
                  + table[base + (h1 * sd + s1) * 3 + channel] * sf;
    return left * (1.f - hf) + right * hf;
}

template<typename T>
static int interpolate(const float* vc, const float* hc, const float* sc,
                       size_t count, const T* table, uint32_t vd,
                       uint32_t hd, uint32_t sd, T* out) {
    if (!vc || !hc || !sc || !table || !out || vd < 1 || vd > 256 ||
        hd < 1 || hd > 360 || sd < 2 || sd > 256 ||
        size_t(vd) * hd * sd > 250000 ||
        count > std::numeric_limits<size_t>::max() / 3) return 1;
    for (size_t i = 0; i < count; ++i) {
        const float v = vc[i], h = hc[i], s = sc[i];
        if (!std::isfinite(v) || !std::isfinite(h) || !std::isfinite(s) ||
            v < 0 || v > vd - 1 || h < 0 || h > hd || s < 0 || s > sd - 1) return 2;
        const auto v0 = size_t(std::floor(v));
        const auto hindex = size_t(std::floor(h));
        const auto s0 = size_t(std::floor(s));
        const float vf = float(double(v) - double(v0));
        const float hf = float(double(h) - double(hindex));
        const float sf = float(double(s) - double(s0));
        const size_t h0 = hindex % hd, h1 = (hindex + 1) % hd;
        const size_t v1 = std::min(v0 + 1, size_t(vd - 1));
        const size_t s1 = std::min(s0 + 1, size_t(sd - 1));
        for (size_t channel = 0; channel < 3; ++channel) {
            T value = plane(table, v0, h0, h1, s0, s1, hd, sd, hf, sf, channel);
            if (vd > 1) {
                value = value * (1.f - vf)
                      + plane(table, v1, h0, h1, s0, s1, hd, sd, hf, sf, channel) * vf;
            }
            out[i * 3 + channel] = value;
        }
    }
    return 0;
}

API int luma_dcp_float(const float* v, const float* h, const float* s,
                      size_t count, const float* table, uint32_t vd,
                      uint32_t hd, uint32_t sd, float* out) {
    return interpolate(v, h, s, count, table, vd, hd, sd, out);
}

API int luma_dcp_double(const float* v, const float* h, const float* s,
                       size_t count, const double* table, uint32_t vd,
                       uint32_t hd, uint32_t sd, double* out) {
    return interpolate(v, h, s, count, table, vd, hd, sd, out);
}
