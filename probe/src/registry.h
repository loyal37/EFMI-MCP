// Registry of everything the game (and XXMI) created on the real device.
// Hashes are computed once, at creation, from the same arguments XXMI sees.
#pragma once
#include "common.h"

enum Origin : uint8_t { ORIGIN_UNKNOWN = 0, ORIGIN_GAME = 1, ORIGIN_XXMI = 2 };
const char* origin_name(uint8_t o);

// One write to a buffer. Every write advances the buffer's epoch by exactly one.
struct WriteRec { uint64_t epoch; uint32_t off, size, frame; };
enum { REG_WRITE_HISTORY = 12 };

// The recent write history of a buffer, newest epoch first in `epoch`.
struct WriteHist {
	uint64_t epoch = 0;
	uint32_t n = 0;
	WriteRec recs[REG_WRITE_HISTORY];
};

struct ResInfo {
	uint8_t  dim = 0;            // D3D11_RESOURCE_DIMENSION
	uint8_t  origin = ORIGIN_UNKNOWN;
	bool     has_init = false;   // initial data took part in the hash
	bool     res_known = false;  // screen resolution was already known at creation
	uint32_t hash = 0;
	uint32_t data_hash = 0;
	uint32_t create_frame = 0;
	uint64_t serial = 0;         // creation order, unique per registry entry
	union {
		D3D11_BUFFER_DESC    buf;
		D3D11_TEXTURE1D_DESC t1;
		D3D11_TEXTURE2D_DESC t2;
		D3D11_TEXTURE3D_DESC t3;
	} desc = {};
	// Write tracking (buffers only, maintained while a client is connected).
	LONG64   epoch = 1;
	LONG     last_write_frame = 0;
	uint8_t  wcount = 0, wpos = 0;
	WriteRec writes[REG_WRITE_HISTORY] = {};
};

struct ShaderInfo {
	uint64_t hash = 0;
	char     type[4] = {};       // "vs", "ps", ...
	uint32_t size = 0;
	uint32_t create_frame = 0;
	uint64_t serial = 0;
	uint8_t  origin = ORIGIN_UNKNOWN;
};

struct LayoutElem {
	char     semantic[40];
	uint32_t index, format, slot, offset, slot_class, step;
};

struct LayoutInfo {
	std::vector<LayoutElem> elems;
	uint64_t serial = 0;
};

void reg_init();

void reg_add_resource(void* ptr, ResInfo& info);          // assigns serial
bool reg_get_resource(void* ptr, ResInfo* out);
// Records a write to [off, off+size) of a tracked buffer (size 0xffffffff:
// to the end / unknown extent). Ignored for anything that is not a known buffer.
void reg_buffer_written(void* ptr, uint32_t off, uint32_t size);
// Current epoch and recent writes of a tracked buffer; false when unknown.
bool reg_write_hist(void* ptr, WriteHist* out);
// Has [off, off+size) been left alone since the buffer was at `since_epoch`?
bool hist_intact(const WriteHist& h, uint32_t off, uint32_t size, uint64_t since_epoch);
// Frame of the latest recorded write touching the range, 0 if none is recorded.
uint32_t hist_last_overlap_frame(const WriteHist& h, uint32_t off, uint32_t size);

void reg_add_shader(void* ptr, const char* type, uint64_t hash, const void* bytecode, size_t len, uint8_t origin);
bool reg_get_shader(void* ptr, ShaderInfo* out);
bool reg_get_bytecode(uint64_t hash, std::vector<uint8_t>* out, std::string* type);

void reg_add_layout(void* ptr, const D3D11_INPUT_ELEMENT_DESC* elems, UINT n);
bool reg_get_layout(void* ptr, LayoutInfo* out);

struct RegStats {
	uint64_t resources, shaders, layouts, bytecode_bytes, bytecode_count, created_total;
};
void reg_stats(RegStats* out);

// Hash of a registry entry as XXMI would report it right now. Textures created
// before the swap chain was discovered are re-derived with the current
// resolution; *assumed is set when that changed the value.
uint32_t reg_effective_hash(const ResInfo& ri, bool* assumed);
