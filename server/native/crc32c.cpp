// Optional game-external CRC32C helper for the Python server. No D3D or hooks.
// A plain C ABI keeps the DLL independent of the Python version.
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <intrin.h>
#include <nmmintrin.h>

#if !defined(_M_X64)
#error Build this optional helper with the MSVC x64 toolchain.
#endif

extern "C" __declspec(dllexport) int efmi_crc32c_supported()
{
    int info[4];
    __cpuid(info, 1);
    return (info[2] & (1 << 20)) != 0; // SSE4.2; checked before the CRC entrypoint.
}

extern "C" __declspec(dllexport) uint32_t efmi_crc32c(
    const void* data, size_t size, uint32_t seed)
{
    const uint8_t* p = static_cast<const uint8_t*>(data);
    uint64_t crc = seed ^ 0xFFFFFFFFu;
    while (size >= sizeof(uint64_t)) {
        uint64_t word;
        memcpy(&word, p, sizeof(word)); // Unaligned input is valid.
        crc = _mm_crc32_u64(crc, word);
        p += sizeof(word);
        size -= sizeof(word);
    }
    uint32_t tail = static_cast<uint32_t>(crc);
    while (size--)
        tail = _mm_crc32_u8(tail, *p++);
    return tail ^ 0xFFFFFFFFu;
}
