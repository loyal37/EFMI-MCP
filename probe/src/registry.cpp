#include "registry.h"
#include "hash.h"
#include <unordered_map>
#include <string.h>

static SRWLOCK g_res_lock = SRWLOCK_INIT;
static std::unordered_map<void*, ResInfo>* g_res;

static SRWLOCK g_sh_lock = SRWLOCK_INIT;
static std::unordered_map<void*, ShaderInfo>* g_shaders;

struct Bytecode { std::vector<uint8_t> data; char type[4]; };
static std::unordered_map<uint64_t, Bytecode>* g_bytecode;
static uint64_t g_bytecode_bytes = 0;
static bool g_bytecode_full_logged = false;

static SRWLOCK g_il_lock = SRWLOCK_INIT;
static std::unordered_map<void*, LayoutInfo>* g_layouts;

static volatile LONG64 g_serial = 0;

// Guarded copies: the source is memory handed to D3D by the game, which we only
// trust as far as D3D itself would have read it.
static bool safe_copy(void* dst, const void* src, size_t n)
{
	__try {
		memcpy(dst, src, n);
		return true;
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		return false;
	}
}

static void safe_strcpy(char* dst, size_t cap, const char* src)
{
	dst[0] = 0;
	__try {
		size_t i = 0;
		for (; src && i + 1 < cap && src[i]; i++)
			dst[i] = src[i];
		dst[i] = 0;
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		dst[0] = 0;
	}
}

const char* origin_name(uint8_t o)
{
	switch (o) {
	case ORIGIN_GAME: return "game";
	case ORIGIN_XXMI: return "xxmi";
	default: return "unknown";
	}
}

void reg_init()
{
	g_res = new std::unordered_map<void*, ResInfo>();
	g_shaders = new std::unordered_map<void*, ShaderInfo>();
	g_bytecode = new std::unordered_map<uint64_t, Bytecode>();
	g_layouts = new std::unordered_map<void*, LayoutInfo>();
	g_res->reserve(1 << 15);
	g_shaders->reserve(1 << 13);
}

// ---------------------------------------------------------------- resources
void reg_add_resource(void* ptr, ResInfo& info)
{
	info.serial = (uint64_t)InterlockedIncrement64(&g_serial);
	info.create_frame = (uint32_t)g_frame_no;
	AcquireSRWLockExclusive(&g_res_lock);
	// A new object at a recycled address simply replaces the stale entry.
	ResInfo& slot = (*g_res)[ptr];
	memcpy(&slot, &info, sizeof(ResInfo));
	ReleaseSRWLockExclusive(&g_res_lock);
}

bool reg_get_resource(void* ptr, ResInfo* out)
{
	bool found = false;
	AcquireSRWLockShared(&g_res_lock);
	auto it = g_res->find(ptr);
	if (it != g_res->end()) {
		memcpy(out, &it->second, sizeof(ResInfo));
		found = true;
	}
	ReleaseSRWLockShared(&g_res_lock);
	return found;
}

// Takes the lock exclusively: the game may use the immediate context from more
// than one thread (under its own lock), and a history entry must not be torn.
void reg_buffer_written(void* ptr, uint32_t off, uint32_t size)
{
	AcquireSRWLockExclusive(&g_res_lock);
	auto it = g_res->find(ptr);
	if (it != g_res->end() && it->second.dim == D3D11_RESOURCE_DIMENSION_BUFFER) {
		ResInfo& r = it->second;
		uint64_t e = (uint64_t)InterlockedIncrement64((volatile LONG64*)&r.epoch);
		r.last_write_frame = g_frame_no;
		WriteRec& w = r.writes[r.wpos];
		w.epoch = e;
		w.off = off;
		w.size = size;
		w.frame = (uint32_t)g_frame_no;
		r.wpos = (uint8_t)((r.wpos + 1) % REG_WRITE_HISTORY);
		if (r.wcount < REG_WRITE_HISTORY)
			r.wcount++;
	}
	ReleaseSRWLockExclusive(&g_res_lock);
}

bool reg_write_hist(void* ptr, WriteHist* out)
{
	bool found = false;
	AcquireSRWLockShared(&g_res_lock);
	auto it = g_res->find(ptr);
	if (it != g_res->end() && it->second.dim == D3D11_RESOURCE_DIMENSION_BUFFER) {
		const ResInfo& r = it->second;
		out->epoch = (uint64_t)r.epoch;
		out->n = r.wcount;
		memcpy(out->recs, r.writes, sizeof(WriteRec) * r.wcount);
		found = true;
	}
	ReleaseSRWLockShared(&g_res_lock);
	return found;
}

static inline bool ranges_overlap(const WriteRec& w, uint32_t off, uint32_t size)
{
	uint64_t w_end = (uint64_t)w.off + w.size;
	uint64_t r_end = (uint64_t)off + size;
	return (uint64_t)w.off < r_end && (uint64_t)off < w_end;
}

bool hist_intact(const WriteHist& h, uint32_t off, uint32_t size, uint64_t since_epoch)
{
	if (!since_epoch || since_epoch > h.epoch)
		return false;
	if (since_epoch == h.epoch)
		return true;
	// Every write since then must still be in the history to be judged.
	if (h.epoch - since_epoch > h.n)
		return false;
	for (uint32_t i = 0; i < h.n; i++)
		if (h.recs[i].epoch > since_epoch && ranges_overlap(h.recs[i], off, size))
			return false;
	return true;
}

uint32_t hist_last_overlap_frame(const WriteHist& h, uint32_t off, uint32_t size)
{
	uint32_t frame = 0;
	for (uint32_t i = 0; i < h.n; i++)
		if (ranges_overlap(h.recs[i], off, size) && h.recs[i].frame > frame)
			frame = h.recs[i].frame;
	return frame;
}

uint32_t reg_effective_hash(const ResInfo& ri, bool* assumed)
{
	if (assumed) *assumed = false;
	if (ri.res_known)
		return ri.hash;
	bool adjusted = false;
	uint32_t h = ri.hash;
	if (ri.dim == D3D11_RESOURCE_DIMENSION_TEXTURE2D)
		h = hash_tex2d_desc(ri.data_hash, &ri.desc.t2, &adjusted);
	else if (ri.dim == D3D11_RESOURCE_DIMENSION_TEXTURE3D)
		h = hash_tex3d_desc(ri.data_hash, &ri.desc.t3, &adjusted);
	if (assumed) *assumed = adjusted;
	return h;
}

// ---------------------------------------------------------------- shaders
void reg_add_shader(void* ptr, const char* type, uint64_t hash, const void* bytecode, size_t len, uint8_t origin)
{
	ShaderInfo si;
	si.hash = hash;
	strncpy_s(si.type, type, _TRUNCATE);
	si.size = (uint32_t)len;
	si.create_frame = (uint32_t)g_frame_no;
	si.serial = (uint64_t)InterlockedIncrement64(&g_serial);
	si.origin = origin;

	AcquireSRWLockExclusive(&g_sh_lock);
	(*g_shaders)[ptr] = si;
	if (g_cfg.keep_bytecode && bytecode && len && g_bytecode->find(hash) == g_bytecode->end()) {
		if (g_bytecode_bytes + len <= (uint64_t)g_cfg.bytecode_cap_mb * 1024 * 1024) {
			Bytecode& b = (*g_bytecode)[hash];
			b.data.resize(len);
			if (!safe_copy(b.data.data(), bytecode, len))
				b.data.clear();
			strncpy_s(b.type, type, _TRUNCATE);
			g_bytecode_bytes += b.data.size();
		} else if (!g_bytecode_full_logged) {
			g_bytecode_full_logged = true;
			plog("registry: bytecode store is full (%u MB), newer shaders are not kept", g_cfg.bytecode_cap_mb);
		}
	}
	ReleaseSRWLockExclusive(&g_sh_lock);
}

bool reg_get_shader(void* ptr, ShaderInfo* out)
{
	bool found = false;
	AcquireSRWLockShared(&g_sh_lock);
	auto it = g_shaders->find(ptr);
	if (it != g_shaders->end()) {
		*out = it->second;
		found = true;
	}
	ReleaseSRWLockShared(&g_sh_lock);
	return found;
}

bool reg_get_bytecode(uint64_t hash, std::vector<uint8_t>* out, std::string* type)
{
	bool found = false;
	AcquireSRWLockShared(&g_sh_lock);
	auto it = g_bytecode->find(hash);
	if (it != g_bytecode->end()) {
		*out = it->second.data;
		if (type) *type = it->second.type;
		found = true;
	}
	ReleaseSRWLockShared(&g_sh_lock);
	return found;
}

// ---------------------------------------------------------------- input layouts
void reg_add_layout(void* ptr, const D3D11_INPUT_ELEMENT_DESC* elems, UINT n)
{
	LayoutInfo li;
	li.serial = (uint64_t)InterlockedIncrement64(&g_serial);
	if (elems) {
		li.elems.resize(n);
		for (UINT i = 0; i < n; i++) {
			LayoutElem& e = li.elems[i];
			memset(&e, 0, sizeof(e));
			safe_strcpy(e.semantic, sizeof(e.semantic), elems[i].SemanticName);
			e.index = elems[i].SemanticIndex;
			e.format = (uint32_t)elems[i].Format;
			e.slot = elems[i].InputSlot;
			e.offset = elems[i].AlignedByteOffset;
			e.slot_class = (uint32_t)elems[i].InputSlotClass;
			e.step = elems[i].InstanceDataStepRate;
		}
	}
	AcquireSRWLockExclusive(&g_il_lock);
	(*g_layouts)[ptr] = std::move(li);
	ReleaseSRWLockExclusive(&g_il_lock);
}

bool reg_get_layout(void* ptr, LayoutInfo* out)
{
	bool found = false;
	AcquireSRWLockShared(&g_il_lock);
	auto it = g_layouts->find(ptr);
	if (it != g_layouts->end()) {
		*out = it->second;
		found = true;
	}
	ReleaseSRWLockShared(&g_il_lock);
	return found;
}

void reg_stats(RegStats* out)
{
	AcquireSRWLockShared(&g_res_lock);
	out->resources = g_res->size();
	ReleaseSRWLockShared(&g_res_lock);
	AcquireSRWLockShared(&g_sh_lock);
	out->shaders = g_shaders->size();
	out->bytecode_bytes = g_bytecode_bytes;
	out->bytecode_count = g_bytecode->size();
	ReleaseSRWLockShared(&g_sh_lock);
	AcquireSRWLockShared(&g_il_lock);
	out->layouts = g_layouts->size();
	ReleaseSRWLockShared(&g_il_lock);
	out->created_total = (uint64_t)g_serial;
}
