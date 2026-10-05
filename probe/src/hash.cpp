#include "hash.h"
#include <intrin.h>
#include <nmmintrin.h>
#include <string.h>
#include <algorithm>

using std::max;
using std::min;

// ============================================================== CRC-32C
static uint32_t g_crc_table[8][256];
static bool     g_crc_hw = false;
static bool     g_crc_ready = false;

static void crc_init()
{
	for (uint32_t n = 0; n < 256; n++) {
		uint32_t c = n;
		for (int k = 0; k < 8; k++)
			c = (c & 1) ? (c >> 1) ^ 0x82F63B78u : c >> 1;
		g_crc_table[0][n] = c;
	}
	for (uint32_t n = 0; n < 256; n++) {
		uint32_t c = g_crc_table[0][n];
		for (int t = 1; t < 8; t++) {
			c = g_crc_table[0][c & 0xff] ^ (c >> 8);
			g_crc_table[t][n] = c;
		}
	}
	int info[4];
	__cpuid(info, 1);
	g_crc_hw = (info[2] & (1 << 20)) != 0;   // SSE4.2
	g_crc_ready = true;
}

static uint32_t crc_raw(uint32_t crc, const uint8_t* p, size_t len)
{
	if (g_crc_hw) {
		uint64_t c = crc;
		while (len >= 8) {
			uint64_t v;
			memcpy(&v, p, 8);
			c = _mm_crc32_u64(c, v);
			p += 8; len -= 8;
		}
		uint32_t c32 = (uint32_t)c;
		while (len--)
			c32 = _mm_crc32_u8(c32, *p++);
		return c32;
	}
	while (len >= 8) {
		uint32_t a, b;
		memcpy(&a, p, 4); memcpy(&b, p + 4, 4);
		a ^= crc;
		crc = g_crc_table[7][a & 0xff] ^ g_crc_table[6][(a >> 8) & 0xff] ^
		      g_crc_table[5][(a >> 16) & 0xff] ^ g_crc_table[4][a >> 24] ^
		      g_crc_table[3][b & 0xff] ^ g_crc_table[2][(b >> 8) & 0xff] ^
		      g_crc_table[1][(b >> 16) & 0xff] ^ g_crc_table[0][b >> 24];
		p += 8; len -= 8;
	}
	while (len--)
		crc = g_crc_table[0][(crc ^ *p++) & 0xff] ^ (crc >> 8);
	return crc;
}

uint32_t crc32c(uint32_t seed, const void* data, size_t len)
{
	if (!g_crc_ready)
		crc_init();
	if (!data || !len)
		return seed;
	uint32_t out = 0;
	__try {
		out = ~crc_raw(~seed, (const uint8_t*)data, len);
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		// XXMI: "Exception caught while calculating crc32c_hw hash" -> 0
		out = 0;
	}
	return out;
}

// ============================================================== FNV-1 (64 bit)
uint64_t fnv64(const void* data, size_t len)
{
	uint64_t h = 0;
	const uint8_t* p = (const uint8_t*)data;
	const uint8_t* e = p + len;
	__try {
		while (p < e) {
			h *= 0x100000001b3ULL;
			h ^= (uint64_t)*p++;
		}
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		h = 0;
	}
	return h;
}

struct dxbc_header {
	char     signature[4];
	uint32_t hash[4];
	uint32_t one;
	uint32_t size;
	uint32_t num_sections;
};

uint64_t shader_hash(const void* bytecode, size_t len)
{
	if (g_cfg.shader_hash == 1 && len >= sizeof(dxbc_header)) {
		const dxbc_header* h = (const dxbc_header*)bytecode;
		if (!memcmp(h->signature, "DXBC", 4))
			return _byteswap_uint64(h->hash[0] | (uint64_t)h->hash[1] << 32);
	}
	// shader_hash = bytecode is not reproduced (EFMI ships with "3dmigoto").
	return fnv64(bytecode, len);
}

// ============================================================== formats
static const char* const k_fmt_names[] = {
	"UNKNOWN", "R32G32B32A32_TYPELESS", "R32G32B32A32_FLOAT", "R32G32B32A32_UINT", "R32G32B32A32_SINT",
	"R32G32B32_TYPELESS", "R32G32B32_FLOAT", "R32G32B32_UINT", "R32G32B32_SINT",
	"R16G16B16A16_TYPELESS", "R16G16B16A16_FLOAT", "R16G16B16A16_UNORM", "R16G16B16A16_UINT", "R16G16B16A16_SNORM", "R16G16B16A16_SINT",
	"R32G32_TYPELESS", "R32G32_FLOAT", "R32G32_UINT", "R32G32_SINT",
	"R32G8X24_TYPELESS", "D32_FLOAT_S8X24_UINT", "R32_FLOAT_X8X24_TYPELESS", "X32_TYPELESS_G8X24_UINT",
	"R10G10B10A2_TYPELESS", "R10G10B10A2_UNORM", "R10G10B10A2_UINT", "R11G11B10_FLOAT",
	"R8G8B8A8_TYPELESS", "R8G8B8A8_UNORM", "R8G8B8A8_UNORM_SRGB", "R8G8B8A8_UINT", "R8G8B8A8_SNORM", "R8G8B8A8_SINT",
	"R16G16_TYPELESS", "R16G16_FLOAT", "R16G16_UNORM", "R16G16_UINT", "R16G16_SNORM", "R16G16_SINT",
	"R32_TYPELESS", "D32_FLOAT", "R32_FLOAT", "R32_UINT", "R32_SINT",
	"R24G8_TYPELESS", "D24_UNORM_S8_UINT", "R24_UNORM_X8_TYPELESS", "X24_TYPELESS_G8_UINT",
	"R8G8_TYPELESS", "R8G8_UNORM", "R8G8_UINT", "R8G8_SNORM", "R8G8_SINT",
	"R16_TYPELESS", "R16_FLOAT", "D16_UNORM", "R16_UNORM", "R16_UINT", "R16_SNORM", "R16_SINT",
	"R8_TYPELESS", "R8_UNORM", "R8_UINT", "R8_SNORM", "R8_SINT", "A8_UNORM", "R1_UNORM",
	"R9G9B9E5_SHAREDEXP", "R8G8_B8G8_UNORM", "G8R8_G8B8_UNORM",
	"BC1_TYPELESS", "BC1_UNORM", "BC1_UNORM_SRGB", "BC2_TYPELESS", "BC2_UNORM", "BC2_UNORM_SRGB",
	"BC3_TYPELESS", "BC3_UNORM", "BC3_UNORM_SRGB", "BC4_TYPELESS", "BC4_UNORM", "BC4_SNORM",
	"BC5_TYPELESS", "BC5_UNORM", "BC5_SNORM", "B5G6R5_UNORM", "B5G5R5A1_UNORM",
	"B8G8R8A8_UNORM", "B8G8R8X8_UNORM", "R10G10B10_XR_BIAS_A2_UNORM",
	"B8G8R8A8_TYPELESS", "B8G8R8A8_UNORM_SRGB", "B8G8R8X8_TYPELESS", "B8G8R8X8_UNORM_SRGB",
	"BC6H_TYPELESS", "BC6H_UF16", "BC6H_SF16", "BC7_TYPELESS", "BC7_UNORM", "BC7_UNORM_SRGB",
	"AYUV", "Y410", "Y416", "NV12", "P010", "P016", "420_OPAQUE", "YUY2", "Y210", "Y216",
	"NV11", "AI44", "IA44", "P8", "A8P8", "B4G4R4A4_UNORM",
};

const char* dxgi_format_name(DXGI_FORMAT f)
{
	unsigned i = (unsigned)f;
	if (i < _countof(k_fmt_names))
		return k_fmt_names[i];
	return "OTHER";
}

// util.h: dxgi_format_size()
static UINT dxgi_format_size(DXGI_FORMAT format)
{
	switch (format) {
	case DXGI_FORMAT_R32G32B32A32_TYPELESS: case DXGI_FORMAT_R32G32B32A32_FLOAT:
	case DXGI_FORMAT_R32G32B32A32_UINT: case DXGI_FORMAT_R32G32B32A32_SINT:
		return 16;
	case DXGI_FORMAT_R32G32B32_TYPELESS: case DXGI_FORMAT_R32G32B32_FLOAT:
	case DXGI_FORMAT_R32G32B32_UINT: case DXGI_FORMAT_R32G32B32_SINT:
		return 12;
	case DXGI_FORMAT_R16G16B16A16_TYPELESS: case DXGI_FORMAT_R16G16B16A16_FLOAT:
	case DXGI_FORMAT_R16G16B16A16_UNORM: case DXGI_FORMAT_R16G16B16A16_UINT:
	case DXGI_FORMAT_R16G16B16A16_SNORM: case DXGI_FORMAT_R16G16B16A16_SINT:
	case DXGI_FORMAT_R32G32_TYPELESS: case DXGI_FORMAT_R32G32_FLOAT:
	case DXGI_FORMAT_R32G32_UINT: case DXGI_FORMAT_R32G32_SINT:
	case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_D32_FLOAT_S8X24_UINT:
	case DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS: case DXGI_FORMAT_X32_TYPELESS_G8X24_UINT:
		return 8;
	case DXGI_FORMAT_R10G10B10A2_TYPELESS: case DXGI_FORMAT_R10G10B10A2_UNORM:
	case DXGI_FORMAT_R10G10B10A2_UINT: case DXGI_FORMAT_R11G11B10_FLOAT:
	case DXGI_FORMAT_R8G8B8A8_TYPELESS: case DXGI_FORMAT_R8G8B8A8_UNORM:
	case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: case DXGI_FORMAT_R8G8B8A8_UINT:
	case DXGI_FORMAT_R8G8B8A8_SNORM: case DXGI_FORMAT_R8G8B8A8_SINT:
	case DXGI_FORMAT_R16G16_TYPELESS: case DXGI_FORMAT_R16G16_FLOAT:
	case DXGI_FORMAT_R16G16_UNORM: case DXGI_FORMAT_R16G16_UINT:
	case DXGI_FORMAT_R16G16_SNORM: case DXGI_FORMAT_R16G16_SINT:
	case DXGI_FORMAT_R32_TYPELESS: case DXGI_FORMAT_D32_FLOAT:
	case DXGI_FORMAT_R32_FLOAT: case DXGI_FORMAT_R32_UINT: case DXGI_FORMAT_R32_SINT:
	case DXGI_FORMAT_R24G8_TYPELESS: case DXGI_FORMAT_D24_UNORM_S8_UINT:
	case DXGI_FORMAT_R24_UNORM_X8_TYPELESS: case DXGI_FORMAT_X24_TYPELESS_G8_UINT:
	case DXGI_FORMAT_R9G9B9E5_SHAREDEXP: case DXGI_FORMAT_R8G8_B8G8_UNORM:
	case DXGI_FORMAT_G8R8_G8B8_UNORM: case DXGI_FORMAT_B8G8R8A8_UNORM:
	case DXGI_FORMAT_B8G8R8X8_UNORM: case DXGI_FORMAT_R10G10B10_XR_BIAS_A2_UNORM:
	case DXGI_FORMAT_B8G8R8A8_TYPELESS: case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
	case DXGI_FORMAT_B8G8R8X8_TYPELESS: case DXGI_FORMAT_B8G8R8X8_UNORM_SRGB:
		return 4;
	case DXGI_FORMAT_R8G8_TYPELESS: case DXGI_FORMAT_R8G8_UNORM: case DXGI_FORMAT_R8G8_UINT:
	case DXGI_FORMAT_R8G8_SNORM: case DXGI_FORMAT_R8G8_SINT:
	case DXGI_FORMAT_R16_TYPELESS: case DXGI_FORMAT_R16_FLOAT: case DXGI_FORMAT_D16_UNORM:
	case DXGI_FORMAT_R16_UNORM: case DXGI_FORMAT_R16_UINT: case DXGI_FORMAT_R16_SNORM:
	case DXGI_FORMAT_R16_SINT: case DXGI_FORMAT_B5G6R5_UNORM: case DXGI_FORMAT_B5G5R5A1_UNORM:
		return 2;
	case DXGI_FORMAT_R8_TYPELESS: case DXGI_FORMAT_R8_UNORM: case DXGI_FORMAT_R8_UINT:
	case DXGI_FORMAT_R8_SNORM: case DXGI_FORMAT_R8_SINT: case DXGI_FORMAT_A8_UNORM:
		return 1;
	default:
		return 0;
	}
}

// DirectXTK LoaderHelpers::BitsPerPixel()
uint32_t dxgi_bits_per_pixel(DXGI_FORMAT fmt)
{
	switch (fmt) {
	case DXGI_FORMAT_R32G32B32A32_TYPELESS: case DXGI_FORMAT_R32G32B32A32_FLOAT:
	case DXGI_FORMAT_R32G32B32A32_UINT: case DXGI_FORMAT_R32G32B32A32_SINT:
		return 128;
	case DXGI_FORMAT_R32G32B32_TYPELESS: case DXGI_FORMAT_R32G32B32_FLOAT:
	case DXGI_FORMAT_R32G32B32_UINT: case DXGI_FORMAT_R32G32B32_SINT:
		return 96;
	case DXGI_FORMAT_R16G16B16A16_TYPELESS: case DXGI_FORMAT_R16G16B16A16_FLOAT:
	case DXGI_FORMAT_R16G16B16A16_UNORM: case DXGI_FORMAT_R16G16B16A16_UINT:
	case DXGI_FORMAT_R16G16B16A16_SNORM: case DXGI_FORMAT_R16G16B16A16_SINT:
	case DXGI_FORMAT_R32G32_TYPELESS: case DXGI_FORMAT_R32G32_FLOAT:
	case DXGI_FORMAT_R32G32_UINT: case DXGI_FORMAT_R32G32_SINT:
	case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_D32_FLOAT_S8X24_UINT:
	case DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS: case DXGI_FORMAT_X32_TYPELESS_G8X24_UINT:
	case DXGI_FORMAT_Y416: case DXGI_FORMAT_Y210: case DXGI_FORMAT_Y216:
		return 64;
	case DXGI_FORMAT_R10G10B10A2_TYPELESS: case DXGI_FORMAT_R10G10B10A2_UNORM:
	case DXGI_FORMAT_R10G10B10A2_UINT: case DXGI_FORMAT_R11G11B10_FLOAT:
	case DXGI_FORMAT_R8G8B8A8_TYPELESS: case DXGI_FORMAT_R8G8B8A8_UNORM:
	case DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: case DXGI_FORMAT_R8G8B8A8_UINT:
	case DXGI_FORMAT_R8G8B8A8_SNORM: case DXGI_FORMAT_R8G8B8A8_SINT:
	case DXGI_FORMAT_R16G16_TYPELESS: case DXGI_FORMAT_R16G16_FLOAT:
	case DXGI_FORMAT_R16G16_UNORM: case DXGI_FORMAT_R16G16_UINT:
	case DXGI_FORMAT_R16G16_SNORM: case DXGI_FORMAT_R16G16_SINT:
	case DXGI_FORMAT_R32_TYPELESS: case DXGI_FORMAT_D32_FLOAT:
	case DXGI_FORMAT_R32_FLOAT: case DXGI_FORMAT_R32_UINT: case DXGI_FORMAT_R32_SINT:
	case DXGI_FORMAT_R24G8_TYPELESS: case DXGI_FORMAT_D24_UNORM_S8_UINT:
	case DXGI_FORMAT_R24_UNORM_X8_TYPELESS: case DXGI_FORMAT_X24_TYPELESS_G8_UINT:
	case DXGI_FORMAT_R9G9B9E5_SHAREDEXP: case DXGI_FORMAT_R8G8_B8G8_UNORM:
	case DXGI_FORMAT_G8R8_G8B8_UNORM: case DXGI_FORMAT_B8G8R8A8_UNORM:
	case DXGI_FORMAT_B8G8R8X8_UNORM: case DXGI_FORMAT_R10G10B10_XR_BIAS_A2_UNORM:
	case DXGI_FORMAT_B8G8R8A8_TYPELESS: case DXGI_FORMAT_B8G8R8A8_UNORM_SRGB:
	case DXGI_FORMAT_B8G8R8X8_TYPELESS: case DXGI_FORMAT_B8G8R8X8_UNORM_SRGB:
	case DXGI_FORMAT_AYUV: case DXGI_FORMAT_Y410: case DXGI_FORMAT_YUY2:
		return 32;
	case DXGI_FORMAT_P010: case DXGI_FORMAT_P016:
		return 24;
	case DXGI_FORMAT_R8G8_TYPELESS: case DXGI_FORMAT_R8G8_UNORM: case DXGI_FORMAT_R8G8_UINT:
	case DXGI_FORMAT_R8G8_SNORM: case DXGI_FORMAT_R8G8_SINT:
	case DXGI_FORMAT_R16_TYPELESS: case DXGI_FORMAT_R16_FLOAT: case DXGI_FORMAT_D16_UNORM:
	case DXGI_FORMAT_R16_UNORM: case DXGI_FORMAT_R16_UINT: case DXGI_FORMAT_R16_SNORM:
	case DXGI_FORMAT_R16_SINT: case DXGI_FORMAT_B5G6R5_UNORM: case DXGI_FORMAT_B5G5R5A1_UNORM:
	case DXGI_FORMAT_A8P8: case DXGI_FORMAT_B4G4R4A4_UNORM:
		return 16;
	case DXGI_FORMAT_NV12: case DXGI_FORMAT_420_OPAQUE: case DXGI_FORMAT_NV11:
		return 12;
	case DXGI_FORMAT_R8_TYPELESS: case DXGI_FORMAT_R8_UNORM: case DXGI_FORMAT_R8_UINT:
	case DXGI_FORMAT_R8_SNORM: case DXGI_FORMAT_R8_SINT: case DXGI_FORMAT_A8_UNORM:
	case DXGI_FORMAT_AI44: case DXGI_FORMAT_IA44: case DXGI_FORMAT_P8:
	case DXGI_FORMAT_BC2_TYPELESS: case DXGI_FORMAT_BC2_UNORM: case DXGI_FORMAT_BC2_UNORM_SRGB:
	case DXGI_FORMAT_BC3_TYPELESS: case DXGI_FORMAT_BC3_UNORM: case DXGI_FORMAT_BC3_UNORM_SRGB:
	case DXGI_FORMAT_BC5_TYPELESS: case DXGI_FORMAT_BC5_UNORM: case DXGI_FORMAT_BC5_SNORM:
	case DXGI_FORMAT_BC6H_TYPELESS: case DXGI_FORMAT_BC6H_UF16: case DXGI_FORMAT_BC6H_SF16:
	case DXGI_FORMAT_BC7_TYPELESS: case DXGI_FORMAT_BC7_UNORM: case DXGI_FORMAT_BC7_UNORM_SRGB:
		return 8;
	case DXGI_FORMAT_BC1_TYPELESS: case DXGI_FORMAT_BC1_UNORM: case DXGI_FORMAT_BC1_UNORM_SRGB:
	case DXGI_FORMAT_BC4_TYPELESS: case DXGI_FORMAT_BC4_UNORM: case DXGI_FORMAT_BC4_SNORM:
		return 4;
	case DXGI_FORMAT_R1_UNORM:
		return 1;
	default:
		return 0;
	}
}

// DirectXTK LoaderHelpers::GetSurfaceInfo()
void dxgi_surface_info(size_t width, size_t height, DXGI_FORMAT fmt,
                       size_t* out_num_bytes, size_t* out_row_bytes, size_t* out_num_rows)
{
	uint64_t num_bytes = 0, row_bytes = 0, num_rows = 0;
	bool bc = false, packed = false, planar = false;
	size_t bpe = 0;

	switch (fmt) {
	case DXGI_FORMAT_BC1_TYPELESS: case DXGI_FORMAT_BC1_UNORM: case DXGI_FORMAT_BC1_UNORM_SRGB:
	case DXGI_FORMAT_BC4_TYPELESS: case DXGI_FORMAT_BC4_UNORM: case DXGI_FORMAT_BC4_SNORM:
		bc = true; bpe = 8; break;
	case DXGI_FORMAT_BC2_TYPELESS: case DXGI_FORMAT_BC2_UNORM: case DXGI_FORMAT_BC2_UNORM_SRGB:
	case DXGI_FORMAT_BC3_TYPELESS: case DXGI_FORMAT_BC3_UNORM: case DXGI_FORMAT_BC3_UNORM_SRGB:
	case DXGI_FORMAT_BC5_TYPELESS: case DXGI_FORMAT_BC5_UNORM: case DXGI_FORMAT_BC5_SNORM:
	case DXGI_FORMAT_BC6H_TYPELESS: case DXGI_FORMAT_BC6H_UF16: case DXGI_FORMAT_BC6H_SF16:
	case DXGI_FORMAT_BC7_TYPELESS: case DXGI_FORMAT_BC7_UNORM: case DXGI_FORMAT_BC7_UNORM_SRGB:
		bc = true; bpe = 16; break;
	case DXGI_FORMAT_R8G8_B8G8_UNORM: case DXGI_FORMAT_G8R8_G8B8_UNORM: case DXGI_FORMAT_YUY2:
		packed = true; bpe = 4; break;
	case DXGI_FORMAT_Y210: case DXGI_FORMAT_Y216:
		packed = true; bpe = 8; break;
	case DXGI_FORMAT_NV12: case DXGI_FORMAT_420_OPAQUE:
		planar = true; bpe = 2; break;
	case DXGI_FORMAT_P010: case DXGI_FORMAT_P016:
		planar = true; bpe = 4; break;
	default:
		break;
	}

	if (bc) {
		uint64_t nbw = 0, nbh = 0;
		if (width > 0)  nbw = max<uint64_t>(1, ((uint64_t)width + 3) / 4);
		if (height > 0) nbh = max<uint64_t>(1, ((uint64_t)height + 3) / 4);
		row_bytes = nbw * bpe;
		num_rows = nbh;
		num_bytes = row_bytes * nbh;
	} else if (packed) {
		row_bytes = (((uint64_t)width + 1) >> 1) * bpe;
		num_rows = (uint64_t)height;
		num_bytes = row_bytes * height;
	} else if (fmt == DXGI_FORMAT_NV11) {
		row_bytes = (((uint64_t)width + 3) >> 2) * 4;
		num_rows = (uint64_t)height * 2;
		num_bytes = row_bytes * num_rows;
	} else if (planar) {
		row_bytes = (((uint64_t)width + 1) >> 1) * bpe;
		num_bytes = (row_bytes * (uint64_t)height) + ((row_bytes * (uint64_t)height + 1) >> 1);
		num_rows = height + (((uint64_t)height + 1) >> 1);
	} else {
		size_t bpp = dxgi_bits_per_pixel(fmt);
		row_bytes = ((uint64_t)width * bpp + 7) / 8;
		num_rows = (uint64_t)height;
		num_bytes = row_bytes * height;
	}
	if (out_num_bytes) *out_num_bytes = (size_t)num_bytes;
	if (out_row_bytes) *out_row_bytes = (size_t)row_bytes;
	if (out_num_rows)  *out_num_rows = (size_t)num_rows;
}

// ============================================================== resolution
static volatile LONG g_res_w = -1, g_res_h = -1;

void hash_set_resolution(int width, int height)
{
	InterlockedExchange(&g_res_w, width);
	InterlockedExchange(&g_res_h, height);
}

void hash_get_resolution(int* width, int* height)
{
	*width = g_res_w;
	*height = g_res_h;
}

// ResourceHash.cpp: AdjustForConstResolution(). Multi-character constants are
// spelled out so the result does not depend on the compiler.
static bool adjust_for_const_resolution(UINT* hash_width, UINT* hash_height)
{
	int width = (int)*hash_width;
	int height = (int)*hash_height;
	int rw = g_res_w, rh = g_res_h;

	if (!g_cfg.res_from_swap_chain)
		return false;

	UINT tag = 0;
	if (width == rw && height == rh)
		tag = 0x53524553;                 // 'SRES'
	else if (width == rw * 2 && height == rh * 2)
		tag = 0x53522A32;                 // 'SR*2'
	else if (width == rw * 4 && height == rh * 4)
		tag = 0x53522A34;                 // 'SR*4'
	else if (width == rw * 8 && height == rh * 8)
		tag = 0x53522A38;                 // 'SR*8'
	else if (width == rw / 2 && height == rh / 2)
		tag = 0x53522F32;                 // 'SR/2'
	if (!tag)
		return false;
	*hash_width = tag;
	*hash_height = tag;
	return true;
}

uint32_t hash_tex2d_desc(uint32_t data_hash, const D3D11_TEXTURE2D_DESC* desc, bool* adjusted)
{
	D3D11_TEXTURE2D_DESC d = *desc;
	bool a = adjust_for_const_resolution(&d.Width, &d.Height);
	if (adjusted) *adjusted = a;
	return crc32c(data_hash, &d, sizeof(d));
}

uint32_t hash_tex3d_desc(uint32_t data_hash, const D3D11_TEXTURE3D_DESC* desc, bool* adjusted)
{
	D3D11_TEXTURE3D_DESC d = *desc;
	bool a = adjust_for_const_resolution(&d.Width, &d.Height);
	if (adjusted) *adjusted = a;
	return crc32c(data_hash, &d, sizeof(d));
}

// ============================================================== data hashes
static UINT compressed_block_size(DXGI_FORMAT f)
{
	switch (f) {
	case DXGI_FORMAT_BC1_TYPELESS: case DXGI_FORMAT_BC1_UNORM: case DXGI_FORMAT_BC1_UNORM_SRGB:
	case DXGI_FORMAT_BC4_TYPELESS: case DXGI_FORMAT_BC4_UNORM: case DXGI_FORMAT_BC4_SNORM:
		return 8;
	case DXGI_FORMAT_BC2_TYPELESS: case DXGI_FORMAT_BC2_UNORM: case DXGI_FORMAT_BC2_UNORM_SRGB:
	case DXGI_FORMAT_BC3_TYPELESS: case DXGI_FORMAT_BC3_UNORM: case DXGI_FORMAT_BC3_UNORM_SRGB:
	case DXGI_FORMAT_BC5_TYPELESS: case DXGI_FORMAT_BC5_UNORM: case DXGI_FORMAT_BC5_SNORM:
	case DXGI_FORMAT_BC6H_TYPELESS: case DXGI_FORMAT_BC6H_UF16: case DXGI_FORMAT_BC6H_SF16:
	case DXGI_FORMAT_BC7_TYPELESS: case DXGI_FORMAT_BC7_UNORM: case DXGI_FORMAT_BC7_UNORM_SRGB:
		return 16;
	default:
		return 0;
	}
}

static size_t texture2d_length(const D3D11_TEXTURE2D_DESC* d, const D3D11_SUBRESOURCE_DATA* init, UINT level)
{
	UINT mip_width = max(d->Width >> level, (UINT)1);
	UINT mip_height = max(d->Height >> level, (UINT)1);
	UINT block = compressed_block_size(d->Format);
	if (!block)
		return init->SysMemPitch * mip_height;
	UINT pw = (mip_width + 3) & ~0x3u;
	UINT ph = (mip_height + 3) & ~0x3u;
	return pw * ph / 16 * block;
}

static size_t texture3d_length(const D3D11_TEXTURE3D_DESC* d, const D3D11_SUBRESOURCE_DATA* init, UINT level)
{
	UINT mip_width = max(d->Width >> level, (UINT)1);
	UINT mip_height = max(d->Height >> level, (UINT)1);
	UINT mip_depth = max(d->Depth >> level, (UINT)1);
	UINT block = compressed_block_size(d->Format);
	if (!block)
		return init->SysMemSlicePitch * mip_depth;
	UINT pw = (mip_width + 3) & ~0x3u;
	UINT ph = (mip_height + 3) & ~0x3u;
	return pw * ph * mip_depth / 16 * block;
}

// ResourceHash.cpp: hash_tex2d_data() with zero_padding == false.
static uint32_t hash_tex2d_data(uint32_t hash, const void* data, size_t length,
                                const D3D11_TEXTURE2D_DESC* d, bool skip_padding, UINT mapped_row_pitch)
{
	if (!skip_padding)
		return crc32c(hash, data, length);

	size_t slice_pitch, row_pitch, row_count;
	dxgi_surface_info(d->Width, d->Height, d->Format, &slice_pitch, &row_pitch, &row_count);

	const uint8_t* sptr = (const uint8_t*)data;
	size_t msize = min(row_pitch, (size_t)mapped_row_pitch);
	signed remaining = (signed)length;
	for (size_t h = 0; h < row_count && remaining > 0; h++) {
		hash = crc32c(hash, sptr, min(msize, (size_t)(unsigned)remaining));
		sptr += mapped_row_pitch;
		remaining -= (signed)msize;
	}
	return hash;
}

static uint32_t tex2d_data_hash(const D3D11_TEXTURE2D_DESC* d, const D3D11_SUBRESOURCE_DATA* init)
{
	if (!d || !init || !init->pSysMem)
		return 0;

	if (g_cfg.texture_hash)   // CalcTexture2DDataHashAccurate()
		return hash_tex2d_data(0, init[0].pSysMem, INT_MAX, d, true, init[0].SysMemPitch);

	size_t length_v12 = d->Width * d->Height * d->ArraySize;
	size_t length = texture2d_length(d, &init[0], 0);
	if (length_v12 <= length)
		return hash_tex2d_data(0, init[0].pSysMem, length_v12, d, false, init[0].SysMemPitch);

	return hash_tex2d_data(0, init[0].pSysMem, length, d, true, init[0].SysMemPitch);
}

uint32_t hash_buffer(const D3D11_BUFFER_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash)
{
	uint32_t dh = 0, hash = 0;
	if (init && init->pSysMem && desc)
		hash = dh = crc32c(hash, init->pSysMem, desc->ByteWidth);
	if (desc)
		hash = crc32c(hash, desc, sizeof(D3D11_BUFFER_DESC));
	if (data_hash) *data_hash = dh;
	return hash;
}

uint32_t hash_tex1d(const D3D11_TEXTURE1D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash)
{
	uint32_t dh = 0;
	if (desc && init && init->pSysMem) {
		size_t length = dxgi_format_size(desc->Format) * max(desc->Width >> 0, (UINT)1);
		dh = crc32c(0, init[0].pSysMem, length);
	}
	uint32_t hash = dh;
	if (desc)
		hash = crc32c(hash, desc, sizeof(D3D11_TEXTURE1D_DESC));
	if (data_hash) *data_hash = dh;
	return hash;
}

uint32_t hash_tex2d(const D3D11_TEXTURE2D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash)
{
	uint32_t dh = tex2d_data_hash(desc, init);
	uint32_t hash = dh;
	if (desc)
		hash = hash_tex2d_desc(dh, desc, nullptr);
	if (data_hash) *data_hash = dh;
	return hash;
}

uint32_t hash_tex3d(const D3D11_TEXTURE3D_DESC* desc, const D3D11_SUBRESOURCE_DATA* init, uint32_t* data_hash)
{
	uint32_t dh = 0;
	if (desc && init && init->pSysMem) {
		size_t length_v12 = desc->Width * desc->Height * desc->Depth;
		size_t length = texture3d_length(desc, &init[0], 0);
		dh = crc32c(0, init[0].pSysMem, length_v12 <= length ? length_v12 : length);
	}
	uint32_t hash = dh;
	if (desc)
		hash = hash_tex3d_desc(dh, desc, nullptr);
	if (data_hash) *data_hash = dh;
	return hash;
}

// ============================================================== draw regions
void ib_region(DXGI_FORMAT fmt, uint32_t bind_offset, const DrawArgs& a, uint32_t* off, uint32_t* size)
{
	uint32_t stride = (fmt == DXGI_FORMAT_R32_UINT) ? 4 : 2;
	*off = bind_offset + stride * a.first_index;
	*size = stride * a.index_count;
}

void vb_region(uint32_t stride, uint32_t bind_offset, const DrawArgs& a, uint32_t* off, uint32_t* size)
{
	// "0.15 * x + 3" when the vertex count is not known from the draw call.
	uint32_t vertex_count = a.vertex_count > 0 ? a.vertex_count : (3 * a.index_count + 10) / 20 + 3;
	*off = bind_offset + stride * a.first_vertex;
	*size = stride * vertex_count;
}
