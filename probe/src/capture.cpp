#include "capture.h"
#include "registry.h"
#include "json.h"
#include <unordered_map>
#include <algorithm>
#include <deque>
#include <map>
#include <tuple>
#include <string.h>

volatile LONG g_recording = 0;
volatile LONG g_tracking = 0;

// ================================================================ data model
enum { ST_VS = 0, ST_HS, ST_DS, ST_GS, ST_PS, ST_CS, ST_COUNT };
enum { OUT_RTV = 10, OUT_DSV = 11, OUT_CS_UAV = 12 };
enum { RS_PENDING = 0, RS_OK = 1, RS_OOB = 2, RS_SKIPPED = 3, RS_CACHED = 4, RS_VOLATILE = 5 };

union AnyDesc {
	D3D11_BUFFER_DESC    buf;
	D3D11_TEXTURE1D_DESC t1;
	D3D11_TEXTURE2D_DESC t2;
	D3D11_TEXTURE3D_DESC t3;
};

struct CapRes {
	void*    ptr = nullptr;
	bool     known = false;      // creation was observed, so the hash is authoritative
	bool     assumed = false;    // hash assumes the swap chain existed at creation
	uint8_t  dim = 0;
	uint8_t  origin = 0;
	uint32_t hash = 0, data_hash = 0;
	uint64_t epoch = 0;          // buffer write epoch when first seen in this frame
	AnyDesc  live = {};
};

struct CapShader { void* ptr; bool known; ShaderInfo info; };
struct CapLayout { void* ptr; bool known; LayoutInfo info; };
struct CapRegion { int32_t res; uint32_t off, size, hash; uint8_t state; bool clamped; };
struct CapSnap   { int32_t res; uint32_t off, size; uint64_t blob_off; uint8_t state; };

struct VbBind   { uint8_t slot; int32_t res; uint32_t stride, off; int32_t region; };
struct CbBind   { uint8_t stage, slot; int32_t res; uint32_t first, num; int32_t snap; };
struct ViewBind { uint8_t stage, slot; int32_t res; };

struct DrawRec {
	uint32_t index = 0;
	uint8_t  type = 0;
	DrawArgs a;
	uint32_t tg[3] = {};
	int32_t  indirect_res = -1;
	uint32_t indirect_off = 0;
	CallerInfo caller;
	uint32_t topology = 0;
	int32_t  layout = -1;
	int32_t  ib_res = -1;
	uint32_t ib_fmt = 0, ib_off = 0;
	int32_t  ib_region = -1;
	int32_t  shaders[ST_COUNT] = { -1, -1, -1, -1, -1, -1 };
	uint32_t vb_begin = 0, vb_count = 0;
	uint32_t cb_begin = 0, cb_count = 0;
	uint32_t srv_begin = 0, srv_count = 0;
	uint32_t out_begin = 0, out_count = 0;
	float    vp[4] = {};
	bool     has_vp = false;
};

struct RKey {
	void* ptr; uint32_t off, size;
	bool operator==(const RKey& o) const { return ptr == o.ptr && off == o.off && size == o.size; }
};
struct RKeyHash {
	size_t operator()(const RKey& k) const
	{
		uint64_t h = (uint64_t)(uintptr_t)k.ptr * 0x9E3779B97F4A7C15ull;
		h ^= ((uint64_t)k.off << 32 | k.size) * 0xC2B2AE3D27D4EB4Full;
		return (size_t)(h ^ (h >> 29));
	}
};
struct FrameRegion { int32_t idx; uint64_t epoch; };

// A draw-time copy waiting to be read back from its staging chunk.
struct Pending {
	uint8_t  kind;       // 0 = region hash, 1 = constant buffer snapshot
	int32_t  idx;
	uint32_t chunk, off, size;
	RKey     key;
	uint64_t epoch;
	uint32_t gen;
};

struct Capture {
	CaptureRequest rq;
	uint32_t frame = 0;
	uint64_t t_start = 0, t_end = 0;
	int res_w = -1, res_h = -1;

	std::vector<CapRes> res;
	std::unordered_map<void*, int32_t> res_ids;
	std::vector<CapShader> shaders;
	std::unordered_map<void*, int32_t> shader_ids;
	std::vector<CapLayout> layouts;
	std::unordered_map<void*, int32_t> layout_ids;
	std::vector<CapRegion> regions;
	std::unordered_map<RKey, FrameRegion, RKeyHash> frame_regions;
	std::vector<CapSnap> snaps;
	std::unordered_map<RKey, FrameRegion, RKeyHash> frame_snaps;

	std::vector<DrawRec> draws;
	std::vector<VbBind> vbs;
	std::vector<CbBind> cbs;
	std::vector<ViewBind> views;
	std::vector<uint8_t> blob;

	std::vector<IUnknown*> held;
	std::vector<uint32_t> chunks;    // staging chunks owned until the readback is done
	std::vector<Pending> pending;
	size_t   chunk_cursor = 0;       // next chunk to read back
	uint32_t wait_frames = 0;        // frames spent waiting for the GPU
	bool     live = false;           // part of the continuous stream, summarised and dropped
	bool     hash_volatile = false;
	uint32_t volatile_skips = 0;
	uint32_t skipped_draws = 0;
	bool     arena_full = false;
	uint64_t arena_bytes = 0;
	uint32_t cache_hits = 0;
	double   record_ms = 0, finalize_ms = 0;
	DWORD    thread_id = 0;
};

uint32_t cap_frame_of(const Capture& c) { return c.frame; }

// ================================================================ state
static CRITICAL_SECTION g_lock;                  // request / job hand-over
static std::shared_ptr<Capture> g_rec;           // being recorded (render thread)
static std::shared_ptr<Capture> g_last;          // last finished capture, resources still held
static uint64_t g_last_touch = 0;

enum { REQ_NONE = 0, REQ_PENDING = 1, REQ_RECORDING = 2 };
static int g_req_state = REQ_NONE;
static bool g_req_abandoned = false;
static CaptureRequest g_req;
static std::shared_ptr<Capture> g_req_result;
static HANDLE g_req_done = nullptr;

// Continuous mode.
struct LiveItem {
	uint8_t  type, origin;
	bool     has_ib_hash, has_ib_res, has_vs, has_ps;
	uint8_t  ib_state;
	uint32_t ib_hash, ib_res_hash;
	uint32_t index_count, first_index, vertex_count;
	uint64_t vs, ps;
	uint32_t draws, instances;
};
struct LiveFrame {
	uint32_t frame = 0;
	uint64_t t_ms = 0;
	uint32_t draws = 0, game_draws = 0, volatile_skips = 0;
	double   cost_ms = 0;
	uint64_t arena_bytes = 0;
	std::vector<LiveItem> items;
};
static LiveConfig g_live;                         // guarded by g_lock
static volatile LONG g_live_on = 0;
static std::deque<LiveFrame> g_live_ring;         // guarded by g_lock

static volatile LONG g_client_gen = 0;           // bumped whenever a client (re)connects
static LONG g_seen_client_gen = 0;
static uint32_t g_track_gen = 1;

struct RVal { uint32_t hash; uint64_t epoch; uint32_t gen; };
static std::unordered_map<RKey, RVal, RKeyHash> g_rcache;

// Buffers the GPU may write to (UAVs, stream output).
static void* g_cs_uav[D3D11_1_UAV_SLOT_COUNT];
static void* g_om_uav[D3D11_1_UAV_SLOT_COUNT];
static void* g_so[D3D11_SO_BUFFER_SLOT_COUNT];
static bool  g_cs_uav_any = false, g_om_any = false;

static LARGE_INTEGER g_qpf;
static double qpc_ms()
{
	LARGE_INTEGER t;
	QueryPerformanceCounter(&t);
	return (double)t.QuadPart * 1000.0 / (double)g_qpf.QuadPart;
}

void cap_init()
{
	InitializeCriticalSection(&g_lock);
	g_req_done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
	QueryPerformanceFrequency(&g_qpf);
}

// ================================================================ arena
// Staging buffers that receive draw-time copies. The copies are queued on the
// GPU while the frame is drawn and are read back later, once the GPU has got
// to them, so the render thread never waits for the GPU.
struct ArenaChunk { ID3D11Buffer* buf; uint32_t cap, used; bool busy; };
static std::vector<ArenaChunk> g_chunks;
static uint64_t g_arena_alloc = 0;

static ID3D11Buffer* create_staging(uint32_t bytes)
{
	D3D11_BUFFER_DESC d;
	memset(&d, 0, sizeof(d));
	d.ByteWidth = bytes;
	d.Usage = D3D11_USAGE_STAGING;
	d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
	ID3D11Buffer* b = nullptr;
	if (FAILED(ODEV(CreateBuffer)(rdev(), &d, nullptr, &b)))
		return nullptr;
	return b;
}

// Releases the chunks nobody is waiting on.
static void arena_free_idle()
{
	for (auto& c : g_chunks) {
		if (c.buf && !c.busy) {
			ID3D11Buffer_Release(c.buf);
			g_arena_alloc -= c.cap;
			c.buf = nullptr;
			c.cap = c.used = 0;
		}
	}
	while (!g_chunks.empty() && !g_chunks.back().buf)
		g_chunks.pop_back();
}

static bool arena_copy(Capture* c, ID3D11Buffer* src, uint32_t off, uint32_t size, uint32_t* chunk, uint32_t* at)
{
	int ci = -1;
	if (!c->chunks.empty()) {
		uint32_t last = c->chunks.back();
		if (g_chunks[last].cap - g_chunks[last].used >= size)
			ci = (int)last;
	}
	if (ci < 0) {
		for (size_t i = 0; i < g_chunks.size(); i++) {
			if (g_chunks[i].buf && !g_chunks[i].busy && g_chunks[i].cap >= size) { ci = (int)i; break; }
		}
		if (ci < 0) {
			uint32_t want = g_cfg.arena_chunk_mb * 1024 * 1024;
			if (size > want)
				want = (size + 0xFFFFu) & ~0xFFFFu;
			if (g_arena_alloc + want > (uint64_t)g_cfg.arena_cap_mb * 1024 * 1024)
				return false;
			ID3D11Buffer* b = create_staging(want);
			if (!b)
				return false;
			for (size_t i = 0; i < g_chunks.size() && ci < 0; i++)
				if (!g_chunks[i].buf) ci = (int)i;
			if (ci < 0) {
				g_chunks.push_back({ nullptr, 0, 0, false });
				ci = (int)g_chunks.size() - 1;
			}
			g_chunks[ci] = { b, want, 0, false };
			g_arena_alloc += want;
		}
		g_chunks[ci].busy = true;
		g_chunks[ci].used = 0;
		c->chunks.push_back((uint32_t)ci);
	}
	ArenaChunk& ch = g_chunks[ci];
	D3D11_BOX box = { off, 0, 0, off + size, 1, 1 };
	OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)ch.buf, 0, ch.used, 0, 0,
	                            (ID3D11Resource*)src, 0, &box);
	*chunk = (uint32_t)ci;
	*at = ch.used;
	ch.used += size;
	c->arena_bytes += size;
	return true;
}

// ================================================================ write tracking
void cap_buffer_written(void* resource, uint32_t off, uint32_t size)
{
	if (resource)
		reg_buffer_written(resource, off, size);
}

static void* view_resource(ID3D11View* v)
{
	if (!v) return nullptr;
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(v, &r);
	if (r) ID3D11Resource_Release(r);   // identity only, no reference kept
	return r;
}

void cap_set_cs_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs)
{
	for (UINT i = 0; i < n && start + i < _countof(g_cs_uav); i++)
		g_cs_uav[start + i] = uavs ? view_resource((ID3D11View*)uavs[i]) : nullptr;
	g_cs_uav_any = false;
	for (void* p : g_cs_uav) if (p) { g_cs_uav_any = true; break; }
}

static void refresh_om_any()
{
	g_om_any = false;
	for (void* p : g_om_uav) if (p) { g_om_any = true; return; }
	for (void* p : g_so) if (p) { g_om_any = true; return; }
}

void cap_set_om_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs)
{
	memset(g_om_uav, 0, sizeof(g_om_uav));
	for (UINT i = 0; i < n && start + i < _countof(g_om_uav); i++)
		g_om_uav[start + i] = uavs ? view_resource((ID3D11View*)uavs[i]) : nullptr;
	refresh_om_any();
}

void cap_set_so_targets(UINT n, ID3D11Buffer* const* targets)
{
	memset(g_so, 0, sizeof(g_so));
	for (UINT i = 0; i < n && i < _countof(g_so); i++)
		g_so[i] = targets ? targets[i] : nullptr;
	refresh_om_any();
}

void cap_state_cleared()
{
	memset(g_cs_uav, 0, sizeof(g_cs_uav));
	memset(g_om_uav, 0, sizeof(g_om_uav));
	memset(g_so, 0, sizeof(g_so));
	g_cs_uav_any = g_om_any = false;
}

void cap_gpu_writes_cs()
{
	if (!g_cs_uav_any) return;
	for (void* p : g_cs_uav) if (p) reg_buffer_written(p, 0, 0xffffffffu);
}

void cap_gpu_writes_om()
{
	if (!g_om_any) return;
	for (void* p : g_om_uav) if (p) reg_buffer_written(p, 0, 0xffffffffu);
	for (void* p : g_so) if (p) reg_buffer_written(p, 0, 0xffffffffu);
}

void cap_invalidate_all()
{
	g_track_gen++;
	g_rcache.clear();
}

void cap_client_connected()
{
	InterlockedIncrement(&g_client_gen);
	InterlockedExchange(&g_tracking, 1);
}

void cap_client_disconnected()
{
	InterlockedExchange(&g_live_on, 0);
	InterlockedExchange(&g_tracking, 0);
	InterlockedIncrement(&g_client_gen);
}

// Bindings made before tracking was switched on are unknown; ask the runtime.
static void resync_gpu_writers()
{
	cap_state_cleared();
	ID3D11UnorderedAccessView* uavs[D3D11_1_UAV_SLOT_COUNT];
	memset(uavs, 0, sizeof(uavs));
	UINT n = g_feature_level >= D3D_FEATURE_LEVEL_11_1 ? D3D11_1_UAV_SLOT_COUNT : D3D11_PS_CS_UAV_REGISTER_COUNT;
	OCTX(CSGetUnorderedAccessViews)(rctx(), 0, n, uavs);
	for (UINT i = 0; i < n; i++) {
		if (!uavs[i]) continue;
		g_cs_uav[i] = view_resource((ID3D11View*)uavs[i]);
		ID3D11UnorderedAccessView_Release(uavs[i]);
		g_cs_uav_any = true;
	}
	ID3D11Buffer* so[D3D11_SO_BUFFER_SLOT_COUNT];
	memset(so, 0, sizeof(so));
	OCTX(SOGetTargets)(rctx(), D3D11_SO_BUFFER_SLOT_COUNT, so);
	for (UINT i = 0; i < D3D11_SO_BUFFER_SLOT_COUNT; i++) {
		if (!so[i]) continue;
		g_so[i] = so[i];
		ID3D11Buffer_Release(so[i]);
	}
	refresh_om_any();
}

// ================================================================ interning
static int32_t res_id(Capture* c, ID3D11Resource* r)
{
	if (!r) return -1;
	auto it = c->res_ids.find(r);
	if (it != c->res_ids.end())
		return it->second;

	CapRes cr;
	cr.ptr = r;
	D3D11_RESOURCE_DIMENSION dim = D3D11_RESOURCE_DIMENSION_UNKNOWN;
	ID3D11Resource_GetType(r, &dim);
	cr.dim = (uint8_t)dim;
	switch (dim) {
	case D3D11_RESOURCE_DIMENSION_BUFFER:    ID3D11Buffer_GetDesc((ID3D11Buffer*)r, &cr.live.buf); break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE1D: ID3D11Texture1D_GetDesc((ID3D11Texture1D*)r, &cr.live.t1); break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE2D: ID3D11Texture2D_GetDesc((ID3D11Texture2D*)r, &cr.live.t2); break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE3D: ID3D11Texture3D_GetDesc((ID3D11Texture3D*)r, &cr.live.t3); break;
	default: break;
	}

	ResInfo ri;
	bool known = reg_get_resource(r, &ri) && ri.dim == cr.dim;
	if (known) {
		// Guard against a recycled address whose new owner we never saw created.
		switch (dim) {
		case D3D11_RESOURCE_DIMENSION_BUFFER:
			known = ri.desc.buf.ByteWidth == cr.live.buf.ByteWidth && ri.desc.buf.BindFlags == cr.live.buf.BindFlags;
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE2D:
			known = ri.desc.t2.Width == cr.live.t2.Width && ri.desc.t2.Height == cr.live.t2.Height &&
			        ri.desc.t2.Format == cr.live.t2.Format;
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE3D:
			known = ri.desc.t3.Width == cr.live.t3.Width && ri.desc.t3.Height == cr.live.t3.Height &&
			        ri.desc.t3.Depth == cr.live.t3.Depth && ri.desc.t3.Format == cr.live.t3.Format;
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE1D:
			known = ri.desc.t1.Width == cr.live.t1.Width && ri.desc.t1.Format == cr.live.t1.Format;
			break;
		default: break;
		}
	}
	cr.known = known;
	if (known) {
		cr.hash = reg_effective_hash(ri, &cr.assumed);
		cr.data_hash = ri.data_hash;
		cr.origin = ri.origin;
		cr.epoch = (uint64_t)ri.epoch;
	}
	if (c->rq.hold) {
		ID3D11Resource_AddRef(r);
		c->held.push_back((IUnknown*)r);
	}
	int32_t id = (int32_t)c->res.size();
	c->res.push_back(cr);
	c->res_ids.emplace(r, id);
	return id;
}

static int32_t view_res_id(Capture* c, ID3D11View* v)
{
	if (!v) return -1;
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(v, &r);
	if (!r) return -1;
	int32_t id = res_id(c, r);
	ID3D11Resource_Release(r);
	return id;
}

static int32_t shader_id(Capture* c, void* sh)
{
	if (!sh) return -1;
	auto it = c->shader_ids.find(sh);
	if (it != c->shader_ids.end())
		return it->second;
	CapShader cs;
	cs.ptr = sh;
	cs.known = reg_get_shader(sh, &cs.info);
	int32_t id = (int32_t)c->shaders.size();
	c->shaders.push_back(cs);
	c->shader_ids.emplace(sh, id);
	return id;
}

static int32_t layout_id(Capture* c, void* il)
{
	if (!il) return -1;
	auto it = c->layout_ids.find(il);
	if (it != c->layout_ids.end())
		return it->second;
	CapLayout cl;
	cl.ptr = il;
	cl.known = reg_get_layout(il, &cl.info);
	int32_t id = (int32_t)c->layouts.size();
	c->layouts.push_back(std::move(cl));
	c->layout_ids.emplace(il, id);
	return id;
}

// ================================================================ regions
// Hash of the bytes a draw reads from a buffer: crc32c over [off, off+size),
// clamped to the buffer, exactly like XXMI's GetRegionHash().
static int32_t region(Capture* c, ID3D11Buffer* buf, int32_t rid, uint32_t off, uint32_t size)
{
	if (!size || rid < 0)
		return -1;
	const CapRes& cr = c->res[rid];
	uint32_t bw = cr.live.buf.ByteWidth;

	CapRegion r = { rid, off, size, 0, RS_PENDING, false };
	int32_t idx = (int32_t)c->regions.size();
	if (off >= bw) {
		r.state = RS_OOB;
		c->regions.push_back(r);
		return idx;
	}
	if (size > bw - off) {
		size = bw - off;
		r.size = size;
		r.clamped = true;
	}

	RKey key = { buf, off, size };
	WriteHist hist;
	bool tracked = cr.known && reg_write_hist(buf, &hist);
	uint64_t epoch = tracked ? hist.epoch : 0;

	if (tracked) {
		// Valid as long as nothing wrote into this byte range since; writes to
		// other parts of a shared buffer do not matter.
		auto f = c->frame_regions.find(key);
		if (f != c->frame_regions.end() && hist_intact(hist, off, size, f->second.epoch))
			return f->second.idx;
		if (g_tracking) {
			auto p = g_rcache.find(key);
			if (p != g_rcache.end() && p->second.gen == g_track_gen && hist_intact(hist, off, size, p->second.epoch)) {
				p->second.epoch = epoch;
				r.hash = p->second.hash;
				r.state = RS_CACHED;
				c->cache_hits++;
				c->regions.push_back(r);
				c->frame_regions[key] = { idx, epoch };
				return idx;
			}
		}
	}

	// The continuous stream does not chase buffers that are being rewritten
	// right now: reading them back would stall every frame, and their hashes
	// change from frame to frame anyway.
	uint32_t last_write = tracked ? hist_last_overlap_frame(hist, off, size) : 0;
	if (c->live && !c->hash_volatile && last_write && (uint32_t)g_frame_no - last_write <= 1) {
		r.state = RS_VOLATILE;
		c->volatile_skips++;
		c->regions.push_back(r);
		c->frame_regions[key] = { idx, epoch };
		return idx;
	}

	uint32_t chunk = 0, at = 0;
	if (!arena_copy(c, buf, off, size, &chunk, &at)) {
		r.state = RS_SKIPPED;
		c->arena_full = true;
		c->regions.push_back(r);
		return idx;
	}
	c->regions.push_back(r);
	c->pending.push_back({ 0, idx, chunk, at, size, key, epoch, g_track_gen });
	if (epoch)
		c->frame_regions[key] = { idx, epoch };
	return idx;
}

// Contents of a constant buffer window as bound for this draw.
static int32_t snapshot(Capture* c, ID3D11Buffer* buf, int32_t rid, uint32_t off, uint32_t size)
{
	if (rid < 0) return -1;
	const CapRes& cr = c->res[rid];
	uint32_t bw = cr.live.buf.ByteWidth;
	if (!size) { off = 0; size = bw; }
	if (off >= bw) return -1;
	if (size > bw - off) size = bw - off;
	if (!size) return -1;

	RKey key = { buf, off, size };
	WriteHist hist;
	bool tracked = cr.known && reg_write_hist(buf, &hist);
	uint64_t epoch = tracked ? hist.epoch : 0;
	if (tracked) {
		auto f = c->frame_snaps.find(key);
		if (f != c->frame_snaps.end() && hist_intact(hist, off, size, f->second.epoch))
			return f->second.idx;
	}
	CapSnap s = { rid, off, size, 0, RS_PENDING };
	int32_t idx = (int32_t)c->snaps.size();
	uint32_t chunk = 0, at = 0;
	if (!arena_copy(c, buf, off, size, &chunk, &at)) {
		s.state = RS_SKIPPED;
		c->arena_full = true;
		c->snaps.push_back(s);
		return idx;
	}
	c->snaps.push_back(s);
	c->pending.push_back({ 1, idx, chunk, at, size, key, epoch, g_track_gen });
	if (epoch)
		c->frame_snaps[key] = { idx, epoch };
	return idx;
}

// ================================================================ per-draw state
static void get_cbs(int stage, ID3D11Buffer** cbs, UINT* first, UINT* num)
{
	const UINT N = D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT;
	if (g_ctx_has_ctx1) {
		ID3D11DeviceContext1* c1 = rctx1();
		switch (stage) {
		case ST_VS: OCTX1(VSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		case ST_HS: OCTX1(HSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		case ST_DS: OCTX1(DSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		case ST_GS: OCTX1(GSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		case ST_PS: OCTX1(PSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		case ST_CS: OCTX1(CSGetConstantBuffers1)(c1, 0, N, cbs, first, num); break;
		}
		return;
	}
	ID3D11DeviceContext* c = rctx();
	switch (stage) {
	case ST_VS: OCTX(VSGetConstantBuffers)(c, 0, N, cbs); break;
	case ST_HS: OCTX(HSGetConstantBuffers)(c, 0, N, cbs); break;
	case ST_DS: OCTX(DSGetConstantBuffers)(c, 0, N, cbs); break;
	case ST_GS: OCTX(GSGetConstantBuffers)(c, 0, N, cbs); break;
	case ST_PS: OCTX(PSGetConstantBuffers)(c, 0, N, cbs); break;
	case ST_CS: OCTX(CSGetConstantBuffers)(c, 0, N, cbs); break;
	}
}

static void get_srvs(int stage, ID3D11ShaderResourceView** srvs)
{
	const UINT N = D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT;
	ID3D11DeviceContext* c = rctx();
	switch (stage) {
	case ST_VS: OCTX(VSGetShaderResources)(c, 0, N, srvs); break;
	case ST_HS: OCTX(HSGetShaderResources)(c, 0, N, srvs); break;
	case ST_DS: OCTX(DSGetShaderResources)(c, 0, N, srvs); break;
	case ST_GS: OCTX(GSGetShaderResources)(c, 0, N, srvs); break;
	case ST_PS: OCTX(PSGetShaderResources)(c, 0, N, srvs); break;
	case ST_CS: OCTX(CSGetShaderResources)(c, 0, N, srvs); break;
	}
}

static void record_stage(Capture* c, DrawRec* d, int stage, bool full)
{
	if (!full)
		return;
	ID3D11Buffer* cbs[D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT];
	UINT first[D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT];
	UINT num[D3D11_COMMONSHADER_CONSTANT_BUFFER_API_SLOT_COUNT];
	memset(cbs, 0, sizeof(cbs));
	memset(first, 0, sizeof(first));
	memset(num, 0, sizeof(num));
	get_cbs(stage, cbs, first, num);
	for (UINT i = 0; i < _countof(cbs); i++) {
		if (!cbs[i]) continue;
		CbBind b;
		b.stage = (uint8_t)stage;
		b.slot = (uint8_t)i;
		b.res = res_id(c, (ID3D11Resource*)cbs[i]);
		b.first = first[i];
		b.num = num[i];
		b.snap = -1;
		if (c->rq.cb_data)
			b.snap = snapshot(c, cbs[i], b.res, first[i] * 16, num[i] * 16);
		c->cbs.push_back(b);
		d->cb_count++;
		ID3D11Buffer_Release(cbs[i]);
	}

	static ID3D11ShaderResourceView* srvs[D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT];
	memset(srvs, 0, sizeof(srvs));
	get_srvs(stage, srvs);
	for (UINT i = 0; i < _countof(srvs); i++) {
		if (!srvs[i]) continue;
		ViewBind v;
		v.stage = (uint8_t)stage;
		v.slot = (uint8_t)i;
		v.res = view_res_id(c, (ID3D11View*)srvs[i]);
		c->views.push_back(v);
		d->srv_count++;
		ID3D11ShaderResourceView_Release(srvs[i]);
	}
}

void cap_on_draw(uint8_t type, const DrawArgs& a, const uint32_t* thread_groups,
                 void* indirect_buffer, uint32_t indirect_offset, const CallerInfo& caller)
{
	Capture* c = g_rec.get();
	if (!c || !g_ctx || !g_dev)
		return;
	uint32_t limit = c->rq.max_draws ? c->rq.max_draws : g_cfg.max_draws;
	if (c->draws.size() >= limit) {
		c->skipped_draws++;
		return;
	}
	double t0 = qpc_ms();
	if (!c->thread_id)
		c->thread_id = GetCurrentThreadId();

	const bool full = c->rq.detail >= 1;
	const bool minimal = c->rq.detail < 0;
	const bool is_dispatch = type == DT_DISPATCH || type == DT_DISPATCH_INDIRECT;
	ID3D11DeviceContext* ctx = rctx();

	DrawRec d;
	d.index = (uint32_t)c->draws.size();
	d.type = type;
	d.a = a;
	d.caller = caller;
	if (thread_groups)
		memcpy(d.tg, thread_groups, sizeof(d.tg));
	if (indirect_buffer) {
		d.indirect_res = res_id(c, (ID3D11Resource*)indirect_buffer);
		d.indirect_off = indirect_offset;
	}
	d.vb_begin = (uint32_t)c->vbs.size();
	d.cb_begin = (uint32_t)c->cbs.size();
	d.srv_begin = (uint32_t)c->views.size();

	if (!is_dispatch) {
		// ---- input assembler
		ID3D11Buffer* ib = nullptr;
		DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN;
		UINT ib_off = 0;
		OCTX(IAGetIndexBuffer)(ctx, &ib, &fmt, &ib_off);
		if (ib) {
			d.ib_res = res_id(c, (ID3D11Resource*)ib);
			d.ib_fmt = (uint32_t)fmt;
			d.ib_off = ib_off;
			if (c->rq.hash_ib && a.index_count) {
				uint32_t ro, rs;
				ib_region(fmt, ib_off, a, &ro, &rs);
				d.ib_region = region(c, ib, d.ib_res, ro, rs);
			}
			ID3D11Buffer_Release(ib);
		}

		ID3D11Buffer* vbs[D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT];
		UINT strides[D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT];
		UINT offsets[D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT];
		memset(vbs, 0, sizeof(vbs));
		memset(strides, 0, sizeof(strides));
		memset(offsets, 0, sizeof(offsets));
		if (!minimal || c->rq.hash_vb)
			OCTX(IAGetVertexBuffers)(ctx, 0, D3D11_IA_VERTEX_INPUT_RESOURCE_SLOT_COUNT, vbs, strides, offsets);
		for (UINT i = 0; i < _countof(vbs); i++) {
			if (!vbs[i]) continue;
			VbBind b;
			b.slot = (uint8_t)i;
			b.res = res_id(c, (ID3D11Resource*)vbs[i]);
			b.stride = strides[i];
			b.off = offsets[i];
			b.region = -1;
			if (c->rq.hash_vb && strides[i]) {
				uint32_t ro, rs;
				vb_region(strides[i], offsets[i], a, &ro, &rs);
				b.region = region(c, vbs[i], b.res, ro, rs);
			}
			c->vbs.push_back(b);
			d.vb_count++;
			ID3D11Buffer_Release(vbs[i]);
		}

		if (!minimal) {
			D3D11_PRIMITIVE_TOPOLOGY topo = D3D11_PRIMITIVE_TOPOLOGY_UNDEFINED;
			OCTX(IAGetPrimitiveTopology)(ctx, &topo);
			d.topology = (uint32_t)topo;

			ID3D11InputLayout* il = nullptr;
			OCTX(IAGetInputLayout)(ctx, &il);
			if (il) {
				d.layout = layout_id(c, il);
				ID3D11InputLayout_Release(il);
			}
		}

		// ---- shaders
		ID3D11VertexShader* vs = nullptr;
		OCTX(VSGetShader)(ctx, &vs, nullptr, nullptr);
		if (vs) { d.shaders[ST_VS] = shader_id(c, vs); ID3D11VertexShader_Release(vs); record_stage(c, &d, ST_VS, full); }
		ID3D11PixelShader* ps = nullptr;
		OCTX(PSGetShader)(ctx, &ps, nullptr, nullptr);
		if (ps) { d.shaders[ST_PS] = shader_id(c, ps); ID3D11PixelShader_Release(ps); record_stage(c, &d, ST_PS, full); }
		if (full) {
			ID3D11HullShader* hs = nullptr;
			OCTX(HSGetShader)(ctx, &hs, nullptr, nullptr);
			if (hs) { d.shaders[ST_HS] = shader_id(c, hs); ID3D11HullShader_Release(hs); record_stage(c, &d, ST_HS, full); }
			ID3D11DomainShader* ds = nullptr;
			OCTX(DSGetShader)(ctx, &ds, nullptr, nullptr);
			if (ds) { d.shaders[ST_DS] = shader_id(c, ds); ID3D11DomainShader_Release(ds); record_stage(c, &d, ST_DS, full); }
			ID3D11GeometryShader* gs = nullptr;
			OCTX(GSGetShader)(ctx, &gs, nullptr, nullptr);
			if (gs) { d.shaders[ST_GS] = shader_id(c, gs); ID3D11GeometryShader_Release(gs); record_stage(c, &d, ST_GS, full); }

			// ---- output merger
			d.out_begin = (uint32_t)c->views.size();
			ID3D11RenderTargetView* rtvs[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT];
			ID3D11DepthStencilView* dsv = nullptr;
			memset(rtvs, 0, sizeof(rtvs));
			OCTX(OMGetRenderTargets)(ctx, D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtvs, &dsv);
			for (UINT i = 0; i < _countof(rtvs); i++) {
				if (!rtvs[i]) continue;
				c->views.push_back({ (uint8_t)OUT_RTV, (uint8_t)i, view_res_id(c, (ID3D11View*)rtvs[i]) });
				d.out_count++;
				ID3D11RenderTargetView_Release(rtvs[i]);
			}
			if (dsv) {
				c->views.push_back({ (uint8_t)OUT_DSV, 0, view_res_id(c, (ID3D11View*)dsv) });
				d.out_count++;
				ID3D11DepthStencilView_Release(dsv);
			}

			UINT nvp = 1;
			D3D11_VIEWPORT vp;
			memset(&vp, 0, sizeof(vp));
			OCTX(RSGetViewports)(ctx, &nvp, &vp);
			if (nvp) {
				d.vp[0] = vp.TopLeftX; d.vp[1] = vp.TopLeftY; d.vp[2] = vp.Width; d.vp[3] = vp.Height;
				d.has_vp = true;
			}
		}
	} else {
		ID3D11ComputeShader* cs = nullptr;
		OCTX(CSGetShader)(ctx, &cs, nullptr, nullptr);
		if (cs) { d.shaders[ST_CS] = shader_id(c, cs); ID3D11ComputeShader_Release(cs); record_stage(c, &d, ST_CS, full); }
		if (full) {
			d.out_begin = (uint32_t)c->views.size();
			ID3D11UnorderedAccessView* uavs[D3D11_1_UAV_SLOT_COUNT];
			memset(uavs, 0, sizeof(uavs));
			UINT n = g_feature_level >= D3D_FEATURE_LEVEL_11_1 ? D3D11_1_UAV_SLOT_COUNT : D3D11_PS_CS_UAV_REGISTER_COUNT;
			OCTX(CSGetUnorderedAccessViews)(ctx, 0, n, uavs);
			for (UINT i = 0; i < n; i++) {
				if (!uavs[i]) continue;
				c->views.push_back({ (uint8_t)OUT_CS_UAV, (uint8_t)i, view_res_id(c, (ID3D11View*)uavs[i]) });
				d.out_count++;
				ID3D11UnorderedAccessView_Release(uavs[i]);
			}
		}
	}

	c->draws.push_back(d);
	c->record_ms += qpc_ms() - t0;
}

// ================================================================ continuous mode
// Collapses a frame into one line per distinct mesh/shader combination.
static void live_push(const Capture& c)
{
	LiveFrame f;
	f.frame = c.frame;
	f.t_ms = c.t_end;
	f.draws = (uint32_t)c.draws.size();
	f.volatile_skips = c.volatile_skips;
	f.cost_ms = c.record_ms + c.finalize_ms;
	f.arena_bytes = c.arena_bytes;

	typedef std::tuple<uint8_t, uint8_t, int32_t, int32_t, uint32_t, uint32_t, uint32_t, int32_t, int32_t> Key;
	std::map<Key, size_t> index;
	for (const DrawRec& d : c.draws) {
		if (d.caller.origin != ORIGIN_XXMI)
			f.game_draws++;
		Key k(d.type, d.caller.origin, d.ib_region, d.ib_res, d.a.index_count, d.a.first_index,
		      d.a.vertex_count, d.shaders[ST_VS], d.shaders[ST_PS]);
		auto it = index.find(k);
		uint32_t inst = d.a.instance_count ? d.a.instance_count : 1;
		if (it != index.end()) {
			f.items[it->second].draws++;
			f.items[it->second].instances += inst;
			continue;
		}
		LiveItem li = {};
		li.type = d.type;
		li.origin = d.caller.origin;
		li.index_count = d.a.index_count;
		li.first_index = d.a.first_index;
		li.vertex_count = d.a.vertex_count;
		li.draws = 1;
		li.instances = inst;
		li.ib_state = 0xff;
		if (d.ib_region >= 0) {
			const CapRegion& r = c.regions[d.ib_region];
			li.ib_state = r.state;
			li.has_ib_hash = (r.state == RS_OK || r.state == RS_CACHED);
			li.ib_hash = r.hash;
		}
		if (d.ib_res >= 0 && c.res[d.ib_res].known) {
			li.has_ib_res = true;
			li.ib_res_hash = c.res[d.ib_res].hash;
		}
		if (d.shaders[ST_VS] >= 0 && c.shaders[d.shaders[ST_VS]].known) {
			li.has_vs = true;
			li.vs = c.shaders[d.shaders[ST_VS]].info.hash;
		}
		if (d.shaders[ST_PS] >= 0 && c.shaders[d.shaders[ST_PS]].known) {
			li.has_ps = true;
			li.ps = c.shaders[d.shaders[ST_PS]].info.hash;
		}
		index.emplace(k, f.items.size());
		f.items.push_back(li);
	}

	EnterCriticalSection(&g_lock);
	g_live_ring.push_back(std::move(f));
	uint32_t keep = g_live.keep_frames ? g_live.keep_frames : 1;
	while (g_live_ring.size() > keep)
		g_live_ring.pop_front();
	LeaveCriticalSection(&g_lock);
}

void cap_live_configure(const LiveConfig& cfg)
{
	EnterCriticalSection(&g_lock);
	g_live = cfg;
	if (g_live.keep_frames > 3600) g_live.keep_frames = 3600;
	if (!g_live.every) g_live.every = 1;
	if (!cfg.enable)
		g_live_ring.clear();
	LeaveCriticalSection(&g_lock);
	InterlockedExchange(&g_live_on, cfg.enable ? 1 : 0);
}

static const char* k_region_state[] = { "pending", "ok", "oob", "skipped", "ok", "volatile" };

void cap_live_get(uint32_t since_frame, uint32_t max_frames, std::string* json)
{
	JsonW w;
	w.begin_arr();
	EnterCriticalSection(&g_lock);
	size_t first = 0;
	while (first < g_live_ring.size() && g_live_ring[first].frame <= since_frame)
		first++;
	if (max_frames && g_live_ring.size() - first > max_frames)
		first = g_live_ring.size() - max_frames;
	for (size_t i = first; i < g_live_ring.size(); i++) {
		const LiveFrame& f = g_live_ring[i];
		w.begin_obj();
		w.ku("frame", f.frame);
		w.ku("t_ms", f.t_ms);
		w.ku("draws", f.draws);
		w.ku("game_draws", f.game_draws);
		w.ku("volatile", f.volatile_skips);
		w.kd("cost_ms", f.cost_ms);
		w.ku("readback_bytes", f.arena_bytes);
		// [type, origin, ib hash, ib state, ib resource hash, index count, first index,
		//  vertex count, vs, ps, draws, instances]
		w.key("items"); w.begin_arr();
		for (const LiveItem& li : f.items) {
			w.begin_arr();
			w.u(li.type);
			w.str(origin_name(li.origin));
			if (li.has_ib_hash) w.hex32(li.ib_hash); else w.null();
			if (li.ib_state == 0xff) w.null(); else w.str(k_region_state[li.ib_state]);
			if (li.has_ib_res) w.hex32(li.ib_res_hash); else w.null();
			w.u(li.index_count); w.u(li.first_index); w.u(li.vertex_count);
			if (li.has_vs) w.hex64(li.vs); else w.null();
			if (li.has_ps) w.hex64(li.ps); else w.null();
			w.u(li.draws); w.u(li.instances);
			w.end_arr();
		}
		w.end_arr();
		w.end_obj();
	}
	LeaveCriticalSection(&g_lock);
	w.end_arr();
	*json += w.s;
}

// ================================================================ frame boundary
static void release_capture_refs(Capture* c)
{
	for (IUnknown* u : c->held)
		IUnknown_Release(u);
	c->held.clear();
}

static std::deque<std::shared_ptr<Capture>> g_finalizing;   // recorded, waiting for the GPU

static void end_recording()
{
	std::shared_ptr<Capture> c = g_rec;
	InterlockedExchange(&g_recording, 0);
	g_rec.reset();
	c->t_end = now_ms();
	g_finalizing.push_back(c);
}

// Collects whatever the GPU has finished copying. Returns true once nothing is
// left. Unless `block` is set this never waits: a chunk the GPU has not got to
// yet is simply tried again on a later frame.
static bool try_readback(Capture* c, bool block)
{
	double t0 = qpc_ms();
	bool done = true;
	while (c->chunk_cursor < c->chunks.size()) {
		uint32_t ci = c->chunks[c->chunk_cursor];
		D3D11_MAPPED_SUBRESOURCE m;
		memset(&m, 0, sizeof(m));
		HRESULT hr = OCTX(Map)(rctx(), (ID3D11Resource*)g_chunks[ci].buf, 0, D3D11_MAP_READ,
		                       block ? 0 : D3D11_MAP_FLAG_DO_NOT_WAIT, &m);
		if (hr == DXGI_ERROR_WAS_STILL_DRAWING) {
			done = false;
			break;
		}
		bool ok = SUCCEEDED(hr) && m.pData;
		if (!ok)
			plog("capture: Map of staging chunk %u failed 0x%08x", ci, (unsigned)hr);
		const uint8_t* base = (const uint8_t*)m.pData;
		for (const Pending& p : c->pending) {
			if (p.chunk != ci)
				continue;
			if (p.kind == 0) {
				CapRegion& r = c->regions[p.idx];
				if (!ok) { r.state = RS_SKIPPED; continue; }
				r.hash = crc32c(0, base + p.off, p.size);
				r.state = RS_OK;
				if (p.epoch && g_tracking && p.gen == g_track_gen) {
					if (g_rcache.size() > 400000)
						g_rcache.clear();
					g_rcache[p.key] = { r.hash, p.epoch, p.gen };
				}
			} else {
				CapSnap& sn = c->snaps[p.idx];
				if (!ok) { sn.state = RS_SKIPPED; continue; }
				sn.blob_off = c->blob.size();
				c->blob.insert(c->blob.end(), base + p.off, base + p.off + p.size);
				sn.state = RS_OK;
			}
		}
		if (ok)
			OCTX(Unmap)(rctx(), (ID3D11Resource*)g_chunks[ci].buf, 0);
		c->chunk_cursor++;
	}
	c->finalize_ms += qpc_ms() - t0;
	return done;
}

static void complete_capture(std::shared_ptr<Capture> c)
{
	for (uint32_t ci : c->chunks) {
		g_chunks[ci].busy = false;
		g_chunks[ci].used = 0;
	}
	c->chunks.clear();
	c->pending.clear();
	c->pending.shrink_to_fit();

	if (c->live) {
		live_push(*c);
		return;
	}

	EnterCriticalSection(&g_lock);
	bool abandoned = g_req_abandoned;
	g_req_state = REQ_NONE;
	g_req_abandoned = false;
	if (!abandoned)
		g_req_result = c;
	LeaveCriticalSection(&g_lock);

	if (abandoned) {
		release_capture_refs(c.get());       // nobody is waiting for it any more
		return;
	}
	// The previous capture's resources are released now that a newer one exists.
	if (g_last)
		release_capture_refs(g_last.get());
	g_last = c;
	g_last_touch = now_ms();
	SetEvent(g_req_done);
}

// Finishes recorded frames in the order they were recorded.
static void process_finalizing()
{
	while (!g_finalizing.empty()) {
		std::shared_ptr<Capture> c = g_finalizing.front();
		// After waiting this long, stop being polite and wait for the GPU.
		bool block = c->wait_frames >= (c->live ? 60u : 20u);
		if (!try_readback(c.get(), block)) {
			c->wait_frames++;
			break;
		}
		g_finalizing.pop_front();
		complete_capture(c);
	}
}

static void begin_recording(const CaptureRequest& rq, bool live, bool hash_volatile)
{
	auto c = std::make_shared<Capture>();
	c->rq = rq;
	c->live = live;
	c->hash_volatile = hash_volatile;
	c->frame = (uint32_t)g_frame_no;
	c->t_start = now_ms();
	hash_get_resolution(&c->res_w, &c->res_h);
	c->draws.reserve(8192);
	c->res.reserve(4096);
	g_rec = c;
	InterlockedExchange(&g_recording, 1);
}

// ---------------------------------------------------------------- jobs
// Reads requested by the client. Like the frame captures they are split in two:
// the copy is issued on one frame and collected on a later one.
struct Job {
	int      type = 0;           // 1 read buffer, 2 read texture, 3 release
	uint32_t frame = 0, res = 0, mip = 0, slice = 0;
	uint64_t offset = 0, size = 0;
	std::shared_ptr<ReadResult> out;
	HANDLE   done = nullptr;
	volatile LONG abandoned = 0; // requester gave up waiting

	// while in flight
	ID3D11Resource* staging = nullptr;
	uint32_t wait_frames = 0;
	uint64_t r_off = 0, r_size = 0, r_bw = 0;
	bool     unchanged_known = false, unchanged = false;
	D3D11_TEXTURE2D_DESC tdesc = {};
	UINT     mw = 0, mh = 0;
	bool     is_1d = false;

	Job() { done = CreateEventW(nullptr, TRUE, FALSE, nullptr); }
	~Job() { if (done) CloseHandle(done); }
};
static std::deque<std::shared_ptr<Job>> g_jobs;        // queued by the IPC thread (g_lock)
static std::vector<std::shared_ptr<Job>> g_inflight;   // copy issued, waiting for the GPU

static const CapRes* job_resource(Job* j, uint8_t dim, uint8_t alt_dim, const char* what)
{
	ReadResult* o = j->out.get();
	Capture* c = g_last.get();
	if (!c || c->frame != j->frame) { o->err = "that capture is no longer held by the probe; capture again"; return nullptr; }
	if (!c->rq.hold || c->held.empty()) { o->err = "that capture was taken without hold, its resources cannot be read"; return nullptr; }
	if (j->res >= c->res.size()) { o->err = "bad resource id"; return nullptr; }
	const CapRes* cr = &c->res[j->res];
	if (cr->dim != dim && cr->dim != alt_dim) { o->err = what; return nullptr; }
	return cr;
}

// Returns true when the copy was issued and the job is now in flight.
static bool start_read_buffer(Job* j)
{
	ReadResult* o = j->out.get();
	const CapRes* cr = job_resource(j, D3D11_RESOURCE_DIMENSION_BUFFER, D3D11_RESOURCE_DIMENSION_BUFFER,
	                                "resource is not a buffer");
	if (!cr) return false;

	uint64_t bw = cr->live.buf.ByteWidth;
	uint64_t off = j->offset, size = j->size;
	if (off >= bw) { o->err = "offset is past the end of the buffer"; return false; }
	if (!size || size > bw - off) size = bw - off;
	if (size > 512ull * 1024 * 1024) { o->err = "read too large"; return false; }

	ID3D11Buffer* st = create_staging((uint32_t)size);
	if (!st) { o->err = "could not create staging buffer"; return false; }
	D3D11_BOX box = { (UINT)off, 0, 0, (UINT)(off + size), 1, 1 };
	OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)st, 0, 0, 0, 0, (ID3D11Resource*)cr->ptr, 0, &box);
	j->staging = (ID3D11Resource*)st;
	j->r_off = off; j->r_size = size; j->r_bw = bw;
	WriteHist hist;
	if (cr->known && cr->epoch && reg_write_hist(cr->ptr, &hist)) {
		j->unchanged_known = true;
		j->unchanged = hist_intact(hist, (uint32_t)off, (uint32_t)size, cr->epoch);
	}
	return true;
}

static bool is_block_compressed(DXGI_FORMAT f)
{
	return (f >= DXGI_FORMAT_BC1_TYPELESS && f <= DXGI_FORMAT_BC5_SNORM) ||
	       (f >= DXGI_FORMAT_BC6H_TYPELESS && f <= DXGI_FORMAT_BC7_UNORM_SRGB);
}

static bool start_read_texture(Job* j)
{
	ReadResult* o = j->out.get();
	const CapRes* cr = job_resource(j, D3D11_RESOURCE_DIMENSION_TEXTURE2D, D3D11_RESOURCE_DIMENSION_TEXTURE1D,
	                                "only 1D and 2D textures (including arrays and cubes) can be read");
	if (!cr) return false;

	if (cr->dim == D3D11_RESOURCE_DIMENSION_TEXTURE1D) {
		// XXMI's IniParams live in a 1D texture, which makes this worth having.
		const D3D11_TEXTURE1D_DESC& d1 = cr->live.t1;
		if (j->mip >= d1.MipLevels || j->slice >= d1.ArraySize) { o->err = "mip or slice out of range"; return false; }
		UINT w1 = std::max(d1.Width >> j->mip, (UINT)1);
		D3D11_TEXTURE1D_DESC s1;
		memset(&s1, 0, sizeof(s1));
		s1.Width = w1;
		s1.MipLevels = 1;
		s1.ArraySize = 1;
		s1.Format = d1.Format;
		s1.Usage = D3D11_USAGE_STAGING;
		s1.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
		ID3D11Texture1D* st1 = nullptr;
		if (FAILED(ODEV(CreateTexture1D)(rdev(), &s1, nullptr, &st1)) || !st1) { o->err = "could not create staging texture"; return false; }
		OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)st1, 0, 0, 0, 0, (ID3D11Resource*)cr->ptr,
		                            j->mip + j->slice * d1.MipLevels, nullptr);
		j->staging = (ID3D11Resource*)st1;
		memset(&j->tdesc, 0, sizeof(j->tdesc));
		j->tdesc.Width = d1.Width;
		j->tdesc.Height = 1;
		j->tdesc.MipLevels = d1.MipLevels;
		j->tdesc.ArraySize = d1.ArraySize;
		j->tdesc.Format = d1.Format;
		j->mw = w1; j->mh = 1;
		j->is_1d = true;
		return true;
	}

	D3D11_TEXTURE2D_DESC d = cr->live.t2;
	if (d.SampleDesc.Count > 1) { o->err = "multisampled textures cannot be read back"; return false; }
	if (j->mip >= d.MipLevels || j->slice >= d.ArraySize) { o->err = "mip or slice out of range"; return false; }

	UINT mw = std::max(d.Width >> j->mip, (UINT)1);
	UINT mh = std::max(d.Height >> j->mip, (UINT)1);
	D3D11_TEXTURE2D_DESC sd;
	memset(&sd, 0, sizeof(sd));
	sd.Width = mw;
	sd.Height = mh;
	if (is_block_compressed(d.Format)) {
		sd.Width = (mw + 3) & ~3u;
		sd.Height = (mh + 3) & ~3u;
	}
	sd.MipLevels = 1;
	sd.ArraySize = 1;
	sd.Format = d.Format;
	sd.SampleDesc.Count = 1;
	sd.Usage = D3D11_USAGE_STAGING;
	sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
	ID3D11Texture2D* st = nullptr;
	HRESULT hr = ODEV(CreateTexture2D)(rdev(), &sd, nullptr, &st);
	if (FAILED(hr) || !st) { o->err = "could not create staging texture"; return false; }

	UINT sub = j->mip + j->slice * d.MipLevels;
	OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)st, 0, 0, 0, 0, (ID3D11Resource*)cr->ptr, sub, nullptr);
	j->staging = (ID3D11Resource*)st;
	j->tdesc = d;
	j->mw = mw; j->mh = mh;
	return true;
}

// Returns true when the job is finished (successfully or not).
static bool finish_read(Job* j, bool block)
{
	ReadResult* o = j->out.get();
	D3D11_MAPPED_SUBRESOURCE m;
	memset(&m, 0, sizeof(m));
	HRESULT hr = OCTX(Map)(rctx(), j->staging, 0, D3D11_MAP_READ, block ? 0 : D3D11_MAP_FLAG_DO_NOT_WAIT, &m);
	if (hr == DXGI_ERROR_WAS_STILL_DRAWING)
		return false;

	if (FAILED(hr) || !m.pData) {
		o->err = "Map failed";
	} else if (j->type == 1) {
		o->data.assign((const uint8_t*)m.pData, (const uint8_t*)m.pData + j->r_size);
		OCTX(Unmap)(rctx(), j->staging, 0);
		JsonW w;
		w.begin_obj();
		w.ku("offset", j->r_off);
		w.ku("size", j->r_size);
		w.ku("byte_width", j->r_bw);
		w.ku("read_frame", (uint32_t)g_frame_no);
		// True when nothing wrote to the buffer between the capture and this read.
		w.key("unchanged_since_capture");
		if (j->unchanged_known) w.b(j->unchanged); else w.null();
		w.end_obj();
		o->meta = w.s;
		o->ok = true;
	} else {
		const D3D11_TEXTURE2D_DESC& d = j->tdesc;
		size_t num_bytes = 0, row_bytes = 0, rows = 0;
		dxgi_surface_info(j->mw, j->mh, d.Format, &num_bytes, &row_bytes, &rows);
		if (row_bytes && rows) {
			o->data.resize(row_bytes * rows);
			size_t copy = std::min(row_bytes, (size_t)m.RowPitch);
			for (size_t r = 0; r < rows; r++)
				memcpy(o->data.data() + r * row_bytes, (const uint8_t*)m.pData + r * m.RowPitch, copy);
			JsonW w;
			w.begin_obj();
			w.ku("width", j->mw);
			w.ku("height", j->mh);
			w.ku("format", (uint32_t)d.Format);
			w.ks("format_name", dxgi_format_name(d.Format));
			w.ku("row_bytes", row_bytes);
			w.ku("rows", rows);
			w.ku("mip", j->mip);
			w.ku("slice", j->slice);
			w.ku("mips", d.MipLevels);
			w.ku("array", d.ArraySize);
			w.ku("tex_width", d.Width);
			w.ku("tex_height", d.Height);
			w.kb("cube", (d.MiscFlags & D3D11_RESOURCE_MISC_TEXTURECUBE) != 0);
			w.ks("kind", j->is_1d ? "tex1d" : "tex2d");
			w.ku("read_frame", (uint32_t)g_frame_no);
			w.end_obj();
			o->meta = w.s;
			o->ok = true;
		} else {
			o->err = "unsupported texture format";
		}
		OCTX(Unmap)(rctx(), j->staging, 0);
	}
	return true;
}

static void drop_staging(Job* j)
{
	if (j->staging) {
		ID3D11Resource_Release(j->staging);
		j->staging = nullptr;
	}
}

static void run_jobs()
{
	// start what was queued
	for (;;) {
		std::shared_ptr<Job> j;
		EnterCriticalSection(&g_lock);
		if (!g_jobs.empty()) {
			j = g_jobs.front();
			g_jobs.pop_front();
		}
		LeaveCriticalSection(&g_lock);
		if (!j)
			break;
		if (j->abandoned)
			continue;
		bool flying = false;
		switch (j->type) {
		case 1: flying = start_read_buffer(j.get()); break;
		case 2: flying = start_read_texture(j.get()); break;
		case 3:
			if (g_last) { release_capture_refs(g_last.get()); g_last.reset(); }
			arena_free_idle();
			break;
		}
		g_last_touch = now_ms();
		if (flying)
			g_inflight.push_back(j);
		else
			SetEvent(j->done);
	}

	// collect what the GPU has finished
	for (size_t i = 0; i < g_inflight.size();) {
		Job* j = g_inflight[i].get();
		bool finished = false;
		if (j->abandoned) {
			finished = true;
		} else {
			finished = finish_read(j, j->wait_frames >= 20);
			if (!finished)
				j->wait_frames++;
		}
		if (finished) {
			drop_staging(j);
			SetEvent(j->done);
			g_inflight.erase(g_inflight.begin() + i);
		} else {
			i++;
		}
	}
}

void cap_on_frame_boundary()
{
	InterlockedIncrement(&g_frame_no);
	if (!g_ctx || !g_dev)
		return;
	hooks_verify();

	if (g_rec)
		end_recording();

	LONG gen = g_client_gen;
	if (gen != g_seen_client_gen) {
		// A client came or went: cached hashes cannot be trusted across the gap.
		g_seen_client_gen = gen;
		g_track_gen++;
		g_rcache.clear();
		if (g_tracking) {
			resync_gpu_writers();
		} else {
			cap_state_cleared();
			if (g_last) { release_capture_refs(g_last.get()); g_last.reset(); }
		}
	}

	process_finalizing();
	run_jobs();

	if (!g_tracking && g_finalizing.empty() && g_inflight.empty())
		arena_free_idle();

	// Do not pin the game's resources forever if the client went quiet.
	if (g_last && !g_last->held.empty() && now_ms() - g_last_touch > 180000) {
		release_capture_refs(g_last.get());
		plog("capture: released resources of frame %u after inactivity", g_last->frame);
	}

	bool start = false, live = false, hash_volatile = false;
	CaptureRequest rq;
	EnterCriticalSection(&g_lock);
	if (g_req_state == REQ_PENDING && !g_rec) {
		// An explicit request takes the next frame; the stream resumes after it.
		rq = g_req;
		start = true;
		g_req_state = REQ_RECORDING;
	} else if (g_live_on && g_tracking && !g_rec && g_finalizing.size() < 8 &&
	           (uint32_t)g_frame_no % g_live.every == 0) {
		rq = CaptureRequest();
		rq.detail = -1;
		rq.hash_ib = g_live.hash_ib;
		rq.hash_vb = g_live.hash_vb;
		rq.cb_data = false;
		rq.hold = false;
		hash_volatile = g_live.hash_volatile;
		start = live = true;
	}
	LeaveCriticalSection(&g_lock);
	if (start)
		begin_recording(rq, live, hash_volatile);
}

// ================================================================ IPC side
std::shared_ptr<Capture> cap_request(const CaptureRequest& rq, std::string* err)
{
	if (!g_ctx || !g_dev) {
		*err = "the probe is not attached to a D3D11 device (it must be loaded before the game creates its device)";
		return nullptr;
	}
	if (!g_swap) {
		*err = "no swap chain seen yet, so frame boundaries are unknown; is the game rendering?";
		return nullptr;
	}
	EnterCriticalSection(&g_lock);
	if (g_req_state != REQ_NONE) {
		LeaveCriticalSection(&g_lock);
		*err = "another capture is already in progress";
		return nullptr;
	}
	g_req = rq;
	g_req_result.reset();
	g_req_abandoned = false;
	g_req_state = REQ_PENDING;
	ResetEvent(g_req_done);
	LeaveCriticalSection(&g_lock);

	DWORD timeout = rq.timeout_ms ? rq.timeout_ms : 5000;
	DWORD w = WaitForSingleObject(g_req_done, timeout);
	if (w != WAIT_OBJECT_0) {
		bool recording = false;
		EnterCriticalSection(&g_lock);
		if (g_req_state == REQ_PENDING)
			g_req_state = REQ_NONE;           // never started: withdraw it
		else if (g_req_state == REQ_RECORDING)
			recording = true;
		LeaveCriticalSection(&g_lock);
		// Started but the closing Present has not arrived: give it a little longer.
		if (recording && WaitForSingleObject(g_req_done, 3000) != WAIT_OBJECT_0) {
			EnterCriticalSection(&g_lock);
			if (g_req_state == REQ_RECORDING)
				g_req_abandoned = true;
			LeaveCriticalSection(&g_lock);
		}
	}
	std::shared_ptr<Capture> out;
	EnterCriticalSection(&g_lock);
	out = g_req_result;
	g_req_result.reset();
	LeaveCriticalSection(&g_lock);
	if (!out)
		*err = "timed out waiting for the game to present a frame (paused, minimised or loading?)";
	return out;
}

static bool run_job(std::shared_ptr<Job> j, uint32_t timeout_ms)
{
	if (!g_ctx || !g_dev)
		return false;
	EnterCriticalSection(&g_lock);
	g_jobs.push_back(j);
	LeaveCriticalSection(&g_lock);
	if (WaitForSingleObject(j->done, timeout_ms ? timeout_ms : 5000) == WAIT_OBJECT_0)
		return true;
	// The render thread may still get to it; make sure it then does nothing.
	InterlockedExchange(&j->abandoned, 1);
	return false;
}

void cap_read_buffer(uint32_t frame, uint32_t res, uint64_t offset, uint64_t size, uint32_t timeout_ms, ReadResult* out)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 1; j->frame = frame; j->res = res; j->offset = offset; j->size = size;
	if (!run_job(j, timeout_ms)) {
		out->err = "timed out waiting for the game to present a frame";
		return;
	}
	*out = std::move(*j->out);
}

void cap_read_texture(uint32_t frame, uint32_t res, uint32_t mip, uint32_t slice, uint32_t timeout_ms, ReadResult* out)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 2; j->frame = frame; j->res = res; j->mip = mip; j->slice = slice;
	if (!run_job(j, timeout_ms)) {
		out->err = "timed out waiting for the game to present a frame";
		return;
	}
	*out = std::move(*j->out);
}

void cap_release(uint32_t timeout_ms)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 3;
	run_job(j, timeout_ms);
}

void cap_status_json(std::string* out)
{
	char b[256];
	snprintf(b, sizeof(b), "\"tracking\":%s,\"recording\":%s,\"live\":%s,\"region_cache\":%zu,\"arena_chunks\":%zu",
	         g_tracking ? "true" : "false", g_recording ? "true" : "false", g_live_on ? "true" : "false",
	         g_rcache.size(), g_chunks.size());
	*out += b;
}

// ================================================================ serialisation
static const char* k_stage[ST_COUNT] = { "vs", "hs", "ds", "gs", "ps", "cs" };

static void write_resource(JsonW& w, uint32_t id, const CapRes& r)
{
	w.begin_obj();
	w.ku("id", id);
	w.key("ptr"); w.ptr(r.ptr);
	w.kb("known", r.known);
	if (r.known) {
		w.ks("origin", origin_name(r.origin));
		w.kh32("hash", r.hash);
		w.kh32("data_hash", r.data_hash);
		if (r.assumed) w.kb("res_assumed", true);
	}
	switch (r.dim) {
	case D3D11_RESOURCE_DIMENSION_BUFFER:
		w.ks("kind", "buffer");
		w.ku("size", r.live.buf.ByteWidth);
		w.ku("usage", r.live.buf.Usage);
		w.ku("bind", r.live.buf.BindFlags);
		w.ku("cpu", r.live.buf.CPUAccessFlags);
		w.ku("misc", r.live.buf.MiscFlags);
		w.ku("stride", r.live.buf.StructureByteStride);
		break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE1D:
		w.ks("kind", "tex1d");
		w.ku("w", r.live.t1.Width);
		w.ku("mips", r.live.t1.MipLevels);
		w.ku("array", r.live.t1.ArraySize);
		w.ku("fmt", (uint32_t)r.live.t1.Format);
		w.ks("fmt_name", dxgi_format_name(r.live.t1.Format));
		w.ku("usage", r.live.t1.Usage);
		w.ku("bind", r.live.t1.BindFlags);
		w.ku("misc", r.live.t1.MiscFlags);
		break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE2D:
		w.ks("kind", "tex2d");
		w.ku("w", r.live.t2.Width);
		w.ku("h", r.live.t2.Height);
		w.ku("mips", r.live.t2.MipLevels);
		w.ku("array", r.live.t2.ArraySize);
		w.ku("fmt", (uint32_t)r.live.t2.Format);
		w.ks("fmt_name", dxgi_format_name(r.live.t2.Format));
		w.ku("samples", r.live.t2.SampleDesc.Count);
		w.ku("usage", r.live.t2.Usage);
		w.ku("bind", r.live.t2.BindFlags);
		w.ku("misc", r.live.t2.MiscFlags);
		break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE3D:
		w.ks("kind", "tex3d");
		w.ku("w", r.live.t3.Width);
		w.ku("h", r.live.t3.Height);
		w.ku("d", r.live.t3.Depth);
		w.ku("mips", r.live.t3.MipLevels);
		w.ku("fmt", (uint32_t)r.live.t3.Format);
		w.ks("fmt_name", dxgi_format_name(r.live.t3.Format));
		w.ku("usage", r.live.t3.Usage);
		w.ku("bind", r.live.t3.BindFlags);
		w.ku("misc", r.live.t3.MiscFlags);
		break;
	default:
		w.ks("kind", "unknown");
	}
	w.end_obj();
}

void cap_serialize(const Capture& c, std::string* json, std::vector<uint8_t>* blob)
{
	JsonW w;
	w.s.reserve(c.draws.size() * 320 + c.res.size() * 220 + 4096);
	w.begin_obj();
	w.ku("v", PROBE_PROTOCOL);
	w.ku("frame", c.frame);
	w.ks("detail", c.rq.detail >= 1 ? "full" : "light");
	w.key("resolution"); w.begin_arr(); w.i(c.res_w); w.i(c.res_h); w.end_arr();
	w.kb("held", !c.held.empty());

	w.key("stats"); w.begin_obj();
	w.ku("draws", c.draws.size());
	w.ku("skipped_draws", c.skipped_draws);
	w.ku("resources", c.res.size());
	w.ku("regions", c.regions.size());
	w.ku("cache_hits", c.cache_hits);
	w.ku("arena_bytes", c.arena_bytes);
	w.kb("arena_full", c.arena_full);
	w.kd("record_ms", c.record_ms);
	w.kd("finalize_ms", c.finalize_ms);
	w.ku("wall_ms", c.t_end - c.t_start);
	w.ku("blob_bytes", c.blob.size());
	w.end_obj();

	w.key("resources"); w.begin_arr();
	for (size_t i = 0; i < c.res.size(); i++)
		write_resource(w, (uint32_t)i, c.res[i]);
	w.end_arr();

	w.key("shaders"); w.begin_arr();
	for (size_t i = 0; i < c.shaders.size(); i++) {
		const CapShader& s = c.shaders[i];
		w.begin_obj();
		w.ku("id", i);
		w.kb("known", s.known);
		if (s.known) {
			w.ks("type", s.info.type);
			w.kh64("hash", s.info.hash);
			w.ku("size", s.info.size);
			w.ks("origin", origin_name(s.info.origin));
		}
		w.end_obj();
	}
	w.end_arr();

	w.key("layouts"); w.begin_arr();
	for (size_t i = 0; i < c.layouts.size(); i++) {
		const CapLayout& l = c.layouts[i];
		w.begin_obj();
		w.ku("id", i);
		w.kb("known", l.known);
		w.key("elems"); w.begin_arr();
		for (const LayoutElem& e : l.info.elems) {
			w.begin_arr();
			w.str(e.semantic); w.u(e.index); w.u(e.format); w.u(e.slot); w.u(e.offset); w.u(e.slot_class); w.u(e.step);
			w.end_arr();
		}
		w.end_arr();
		w.end_obj();
	}
	w.end_arr();

	// [resource, offset, size, hash, state]
	w.key("regions"); w.begin_arr();
	for (const CapRegion& r : c.regions) {
		w.begin_arr();
		w.i(r.res); w.u(r.off); w.u(r.size);
		if (r.state == RS_OK || r.state == RS_CACHED) w.hex32(r.hash); else w.null();
		w.str(k_region_state[r.state]);
		w.end_arr();
	}
	w.end_arr();

	// [resource, offset, size, blob offset]
	w.key("snaps"); w.begin_arr();
	for (const CapSnap& s : c.snaps) {
		w.begin_arr();
		w.i(s.res); w.u(s.off); w.u(s.size);
		if (s.state == RS_OK) w.u(s.blob_off); else w.null();
		w.end_arr();
	}
	w.end_arr();

	w.key("draws"); w.begin_arr();
	for (const DrawRec& d : c.draws) {
		w.begin_obj();
		w.ku("i", d.index);
		w.ku("t", d.type);
		if (d.a.index_count)    w.ku("ic", d.a.index_count);
		if (d.a.vertex_count)   w.ku("vc", d.a.vertex_count);
		if (d.a.instance_count) w.ku("inst", d.a.instance_count);
		if (d.a.first_index)    w.ku("fi", d.a.first_index);
		if (d.a.first_vertex)   w.ki("fv", (int32_t)d.a.first_vertex);
		if (d.a.first_instance) w.ku("finst", d.a.first_instance);
		if (d.type == DT_DISPATCH) { w.key("tg"); w.begin_arr(); w.u(d.tg[0]); w.u(d.tg[1]); w.u(d.tg[2]); w.end_arr(); }
		if (d.indirect_res >= 0) { w.key("ind"); w.begin_arr(); w.i(d.indirect_res); w.u(d.indirect_off); w.end_arr(); }
		w.ks("o", origin_name(d.caller.origin));
		if (d.caller.depth) { w.ku("xd", d.caller.depth); w.ku("ra", d.caller.rva); }
		if (d.type < DT_DISPATCH) {
			w.ku("topo", d.topology);
			w.ki("il", d.layout);
			if (d.ib_res >= 0) {
				w.key("ib"); w.begin_arr();
				w.i(d.ib_res); w.u(d.ib_fmt); w.u(d.ib_off); w.i(d.ib_region);
				w.end_arr();
			}
			if (d.vb_count) {
				w.key("vb"); w.begin_arr();
				for (uint32_t k = 0; k < d.vb_count; k++) {
					const VbBind& b = c.vbs[d.vb_begin + k];
					w.begin_arr(); w.u(b.slot); w.i(b.res); w.u(b.stride); w.u(b.off); w.i(b.region); w.end_arr();
				}
				w.end_arr();
			}
		}
		w.key("sh"); w.begin_arr();
		for (int s = 0; s < ST_COUNT; s++) w.i(d.shaders[s]);
		w.end_arr();
		if (d.cb_count) {
			w.key("cb"); w.begin_arr();
			for (uint32_t k = 0; k < d.cb_count; k++) {
				const CbBind& b = c.cbs[d.cb_begin + k];
				w.begin_arr(); w.u(b.stage); w.u(b.slot); w.i(b.res); w.u(b.first); w.u(b.num); w.i(b.snap); w.end_arr();
			}
			w.end_arr();
		}
		if (d.srv_count) {
			w.key("srv"); w.begin_arr();
			for (uint32_t k = 0; k < d.srv_count; k++) {
				const ViewBind& v = c.views[d.srv_begin + k];
				w.begin_arr(); w.u(v.stage); w.u(v.slot); w.i(v.res); w.end_arr();
			}
			w.end_arr();
		}
		if (d.out_count) {
			w.key("out"); w.begin_arr();
			for (uint32_t k = 0; k < d.out_count; k++) {
				const ViewBind& v = c.views[d.out_begin + k];
				w.begin_arr(); w.u(v.stage); w.u(v.slot); w.i(v.res); w.end_arr();
			}
			w.end_arr();
		}
		if (d.has_vp) {
			w.key("vp"); w.begin_arr();
			w.d(d.vp[0]); w.d(d.vp[1]); w.d(d.vp[2]); w.d(d.vp[3]);
			w.end_arr();
		}
		w.end_obj();
	}
	w.end_arr();
	w.key("stages"); w.begin_arr();
	for (int s = 0; s < ST_COUNT; s++) w.str(k_stage[s]);
	w.end_arr();
	w.end_obj();

	*json = std::move(w.s);
	*blob = c.blob;
}
