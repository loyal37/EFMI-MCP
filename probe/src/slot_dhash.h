#pragma once
#include "common.h"
#include <memory>

// Render-thread-owned. The result becomes ready only after a nonblocking GPU read.
struct SlotDHash {
    uint64_t value = 0;
    uint32_t mip = 0, width = 0, height = 0;
    bool ready = false;
    HRESULT error = S_OK;
};
uint64_t slot_dhash_signature(ID3D11ShaderResourceView* srv, uint64_t ps, unsigned slot);
std::shared_ptr<SlotDHash> slot_dhash_request(ID3D11ShaderResourceView* srv, uint64_t ps, unsigned slot, bool* deferred);
void slot_dhash_boundary();
void slot_dhash_written(ID3D11Resource* resource);
void slot_dhash_unknown_writes();
void slot_dhash_status(std::string* out);
