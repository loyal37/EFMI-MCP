#pragma once
#include <array>
#include <string>

// D3DDisassemble reads executable declarations even when DXBC has no RDEF chunk.
// Accept only plain Texture2D declarations, never arrays/cubes/MSAA or incidental tN uses.
inline std::array<bool, 128> texture2d_declarations(const std::string& assembly)
{
    std::array<bool, 128> slots = {};
    const std::string prefix = "dcl_resource_texture2d ";
    for (size_t begin = 0; begin < assembly.size();) {
        size_t end = assembly.find('\n', begin);
        if (end == std::string::npos) end = assembly.size();
        std::string line = assembly.substr(begin, end - begin);
        begin = end + 1;
        size_t first = line.find_first_not_of(" \t");
        if (first == std::string::npos || line.compare(first, prefix.size(), prefix) != 0) continue;
        size_t close = line.find(')', first + prefix.size());
        if (close == std::string::npos) continue;
        size_t reg = line.find_first_not_of(" \t", close + 1);
        if (reg == std::string::npos || line[reg++] != 't') continue;
        unsigned slot = 0;
        size_t digits = reg;
        while (reg < line.size() && line[reg] >= '0' && line[reg] <= '9' && slot < slots.size())
            slot = slot * 10 + (line[reg++] - '0');
        if (reg == digits || slot >= slots.size()) continue;
        if (line.find_first_not_of(" \t\r", reg) != std::string::npos) continue;
        slots[slot] = true;
    }
    return slots;
}
