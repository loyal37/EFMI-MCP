// Hash functions that reproduce XXMI-Libs (3DMigoto) v1.1.x bit for bit.
// References are to DirectX11/ResourceHash.cpp, HackerDevice.cpp and util.h of
// https://github.com/SpectrumQT/XXMI-Libs-Package (GPLv3). Nothing is copied
// from the DLL; the algorithms are re-implemented from the published source.
#pragma once
#include "common.h"

// crc32c_hw(): standard CRC-32C with an incremental seed. Returns 0 if the
// buffer cannot be read (XXMI swallows the fault the same way).
uint32_t crc32c(uint32_t seed, const void* data, size_t len);

// fnv_64_buf(): FNV-1 with a zero start value, used for shader hashes.
uint64_t fnv64(const void* data, size_t len);

// Shader hash according to [Rendering] shader_hash.
uint64_t shader_hash(const void* bytecode, size_t len);

// Current "screen resolution" as XXMI tracks it (-1 while unknown).
void     hash_set_resolution(int width, int height);
void     hash_get_resolution(int* width, int* height);

uint32_t hash_buffer(const D3D11_BUFFER_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash);
uint32_t hash_tex1d (const D3D11_TEXTURE1D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash);
uint32_t hash_tex2d (const D3D11_TEXTURE2D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash);
uint32_t hash_tex3d (const D3D11_TEXTURE3D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash);

// Desc-only part, used to re-derive a hash once the resolution becomes known.
uint32_t hash_tex2d_desc(uint32_t data_hash, const D3D11_TEXTURE2D_DESC* desc, bool* adjusted);
uint32_t hash_tex3d_desc(uint32_t data_hash, const D3D11_TEXTURE3D_DESC* desc, bool* adjusted);

// Region of a buffer used by a draw call (GetIndexBufferRegion* / GetVertexBufferRegion*).
struct DrawArgs {
	uint32_t type = 0;            // DrawCall enum value, see capture.h
	uint32_t vertex_count = 0, index_count = 0, instance_count = 0;
	uint32_t first_vertex = 0, first_index = 0, first_instance = 0;
};
void ib_region(DXGI_FORMAT fmt, uint32_t bind_offset, const DrawArgs& a, uint32_t* off, uint32_t* size);
void vb_region(uint32_t stride, uint32_t bind_offset, const DrawArgs& a, uint32_t* off, uint32_t* size);

const char* dxgi_format_name(DXGI_FORMAT f);
uint32_t    dxgi_bits_per_pixel(DXGI_FORMAT f);

// DirectXTK LoaderHelpers::GetSurfaceInfo(): tight size of one subresource.
void        dxgi_surface_info(size_t width, size_t height, DXGI_FORMAT fmt,
                              size_t* out_num_bytes, size_t* out_row_bytes, size_t* out_num_rows);
