#include "capture.h"
#include "registry.h"
#include "json.h"
#include "slotcache.h"
#include "record.h"
#include <unordered_map>
#include <unordered_set>
#include <algorithm>
#include <deque>
#include <map>
#include <tuple>
#include <string.h>

volatile LONG g_recording = 0;
volatile LONG g_caller_late = 0;
volatile LONG g_tracking = 0;
volatile LONG g_after = 0;
volatile LONG g_barrier = 0;
volatile LONG g_events = 0;

// ================================================================ data model
enum { ST_VS = 0, ST_HS, ST_DS, ST_GS, ST_PS, ST_CS, ST_COUNT };
enum { OUT_RTV = 10, OUT_DSV = 11, OUT_CS_UAV = 12, OUT_OM_UAV = 13 };
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
	uint32_t create_frame = 0;   // frame counter when the object was created
	uint64_t serial = 0;         // creation order; a new object at a reused address gets a new one
	AnyDesc  live = {};
};

struct CapShader { void* ptr; bool known; ShaderInfo info; };
struct CapLayout { void* ptr; bool known; LayoutInfo info; };
struct CapRegion { int32_t res; uint32_t off, size, hash; uint8_t state; bool clamped; };
struct CapSnap   { int32_t res; uint32_t off, size; uint64_t blob_off; uint8_t state; };

struct VbBind   { uint8_t slot; int32_t res; uint32_t stride, off; int32_t region; int32_t cont = -1; };
struct CbBind   { uint8_t stage, slot; int32_t res; uint32_t first, num; int32_t snap; int32_t cont = -1; };
struct ViewBind { uint8_t stage, slot; int32_t res; int32_t view = -1; int32_t cont = -1; };

// Fixed-function state objects and view parameters, recorded for full captures.
enum { SK_BLEND = 0, SK_DEPTH = 1, SK_RASTER = 2, SK_SAMPLER = 3 };
struct CapState {
	void*   ptr;
	uint8_t kind;
	union {
		D3D11_BLEND_DESC         blend;
		D3D11_DEPTH_STENCIL_DESC depth;
		D3D11_RASTERIZER_DESC    raster;
		D3D11_SAMPLER_DESC       sampler;
	} d;
};
enum { VK_SRV = 0, VK_RTV = 1, VK_DSV = 2, VK_UAV = 3 };
// p: first mip (or element), mip (or element) count, first slice, slice count, as far as the view has them.
struct CapView { void* ptr; int32_t res; uint8_t kind; uint32_t format, dim, p[4], flags; };
struct SamplerBind { uint8_t stage, slot; int32_t state; };

// A copy of a render target taken just before or just after one draw.
enum { RT_OK = 0, RT_NOT_2D = 1, RT_MULTISAMPLED = 2, RT_BUDGET = 3, RT_FAILED = 4 };
struct RtSnap {
	uint32_t draw;
	uint8_t  when;               // 0 before the draw, 1 after it
	uint8_t  kind, slot;         // OUT_RTV / OUT_DSV and its slot
	uint8_t  state;
	int32_t  res;
	uint32_t mip, slice, w, h;
	bool     boxed;
	uint32_t box[4];
	DXGI_FORMAT format;          // the view's when it names one, else the texture's
	D3D11_TEXTURE2D_DESC desc;
	ID3D11Texture2D* staging;
	uint64_t bytes;
};
struct RtAfter { uint8_t kind, slot; ID3D11View* view; };

// ---- contents
// What a call read or wrote, as it was at that moment. A snapshot names one
// version of a buffer, or one subresource of a texture as it was until the
// next write to it.
enum { CS_LIVE = 0, CS_COPIED = 1, CS_BUDGET = 2, CS_MEMORY = 3, CS_FAILED = 4, CS_UNSUPPORTED = 5, CS_MAPPED = 6 };
struct ContSnap {
	int32_t  res;
	uint8_t  tex;                // 0: a buffer version, 1: a texture subresource
	uint8_t  state;              // textures; a buffer answers from its track
	uint8_t  dim;                // of the copy
	bool     resolved;           // the copy holds the resolved samples of a multisampled texture
	uint32_t ver;                // buffers
	uint32_t mip, slice;         // textures
	ID3D11Resource* copy;        // textures: the subresource as it was, once something was about to write to it
	uint32_t w, h, d;
	DXGI_FORMAT format, hint;    // hint: the format of the view it was seen through, to resolve typeless samples
	uint64_t bytes;
};
// Bytes [off, off+size) of a buffer as they were up to and including version `ver`.
struct ContPatch { uint32_t ver, off, size, chunk, at; };
struct ContChunk { ID3D11Buffer* buf; uint32_t cap, used; };
struct ContPending { uint32_t sub; int32_t snap; };
struct ContTrack {
	int32_t  rid = -1;
	bool     buffer = false;
	// buffers
	uint32_t ver = 0;            // every write that follows a reference starts a new version
	int32_t  cur = -1;           // snapshot of the latest version that was referenced
	bool     sealed = false;     // the whole buffer was saved after that reference: nothing more to keep
	bool     lost = false;       // a piece could not be kept: versions up to lost_ver cannot be rebuilt
	uint8_t  lost_why = 0;
	uint32_t lost_ver = 0;
	uint64_t since_ref = 0;      // bytes saved since the latest reference
	std::vector<ContPatch> patches;
	// textures: subresources that a call referenced and nothing has written to since
	std::vector<ContPending> pending;
};
struct OutAfter { uint32_t bind; };          // index into Capture::views of an output to note after the call
struct CapEvent { uint8_t type; uint32_t draw; int32_t dst, src; uint32_t a[4]; float f[4]; bool has_f; };

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
	int32_t  ib_cont = -1;       // contents: the index buffer as it was

	// full captures only
	bool     has_state = false;
	int32_t  blend = -1, depth = -1, raster = -1;
	float    blend_factor[4] = {};
	uint32_t sample_mask = 0, stencil_ref = 0;
	uint32_t smp_begin = 0, smp_count = 0;
	uint32_t sc_begin = 0, sc_count = 0;
	uint32_t vp_count = 0;
	float    vp_z[2] = { 0, 1 };
	uint32_t vp_begin = 0;       // all of them, when there is more than one
	int32_t  indirect_snap = -1;
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

// The slot cache tells draws apart by what is bound, before anything is hashed: the index buffer
// object and its creation serial, the range, and the shaders by creation serial. Not the base
// vertex: the game moves meshes around in shared vertex buffers.
struct SlotSeen {
	void*    ib = nullptr;
	uint64_t ib_sn = 0;
	uint32_t ib_off = 0, ib_fmt = 0, first = 0, count = 0;
	uint64_t vs_sn = 0, ps_sn = 0;
	bool operator==(const SlotSeen& o) const
	{
		return ib == o.ib && ib_sn == o.ib_sn && ib_off == o.ib_off && ib_fmt == o.ib_fmt && first == o.first &&
		       count == o.count && vs_sn == o.vs_sn && ps_sn == o.ps_sn;
	}
};
struct SlotSeenHash {
	size_t operator()(const SlotSeen& k) const
	{
		uint64_t h = (uint64_t)(uintptr_t)k.ib * 0x9E3779B97F4A7C15ull;
		h ^= (k.ib_sn + ((uint64_t)k.first << 32 | k.count)) * 0xC2B2AE3D27D4EB4Full;
		h ^= (k.vs_sn * 31 + k.ps_sn + ((uint64_t)k.ib_off << 20)) * 0x165667B19E3779F9ull;
		return (size_t)(h ^ (h >> 31));
	}
};

// A draw-time copy waiting to be read back from its staging chunk.
struct Pending {
	uint8_t  kind;       // 0 = region hash, 1 = constant buffer snapshot
	int32_t  idx;
	uint32_t chunk, off, size;
	RKey     key;
	uint64_t epoch;
	uint32_t gen;
	uint64_t serial;     // of the buffer object the copy was taken from
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

	std::vector<CapState> states;
	std::unordered_map<void*, int32_t> state_ids;
	std::vector<CapView> view_descs;
	std::unordered_map<void*, int32_t> view_ids;
	std::vector<SamplerBind> samplers;
	std::vector<D3D11_RECT> rects;
	std::vector<D3D11_VIEWPORT> viewports;

	// contents
	std::vector<ContSnap> conts;
	std::unordered_map<void*, ContTrack> tracks;
	std::vector<ContChunk> cchunks;
	std::vector<OutAfter> out_after;  // outputs of the call that is being issued right now
	uint32_t after_draw = 0;
	uint64_t ct_bytes = 0;            // kept so far
	uint32_t ct_copies = 0, ct_lost = 0;
	uint32_t ct_out_calls = 0;        // calls whose outputs were noted
	bool     ct_unknown = false;      // a command list ran: what is read in place may be newer than the call
	bool     ct_released = false;     // the contents were let go of
	std::vector<CapEvent> events;
	uint32_t events_dropped = 0;

	std::vector<RtSnap> rts;
	std::vector<RtAfter> rt_after;   // targets of the draw that is being issued right now
	uint32_t rt_after_draw = 0;
	uint32_t rt_matched = 0;         // draws the request selects
	uint32_t rt_draws = 0;           // of those, the ones whose targets were copied
	uint32_t rt_depth_only = 0;      // selected draws without a colour target while depth was not asked for
	uint32_t rt_skipped = 0;         // copies left out, over the pixel budget
	uint64_t rt_bytes = 0;

	std::vector<IUnknown*> held;
	std::vector<uint32_t> chunks;    // staging chunks owned until the readback is done
	std::vector<Pending> pending;
	size_t   chunk_cursor = 0;       // next chunk to read back
	uint32_t wait_frames = 0;        // frames spent waiting for the GPU
	bool     live = false;           // part of the continuous stream, summarised and dropped
	bool     slots = false;          // looked at for the slot cache: only draws not seen before
	bool     trig = false;           // started by the trigger
	uint32_t trig_id = 0;            // ... of this arming; for a stream frame, the arming that watched it
	bool     rec = false;            // a frame of the recording's stream
	bool     rec_kf = false;         // a keyframe of the recording
	uint32_t rec_id = 0;             // ... of this recording
	const char* rec_reason = "";     // why the keyframe was taken
	std::string rec_what;
	double   t_end_qpc = 0;          // when the frame ended (ms, high resolution)
	uint32_t slot_new = 0;           // draws recorded for it
	uint32_t slot_deferred = 0;      // new draws left for a later frame (over max_new)
	std::vector<SlotSeen> slot_keys; // one per recorded draw
	uint64_t live_generation = 0;    // stream session that requested this frame
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

// Continuous mode. A line of a frame is a mesh/shader combination; the same line comes back in
// frame after frame, so it is kept once (LiveKey, by number) and a frame holds only its counts.
// Keys are built from zeroed memory and have no padding, so that they compare and hash as bytes.
struct LiveKey {
	uint8_t  type, origin;
	bool     has_ib_hash, has_ib_res, has_vs, has_ps;
	uint8_t  ib_state, unused8;
	uint32_t ib_hash, ib_res_hash;
	uint32_t index_count, first_index, vertex_count, unused32;
	uint64_t vs, ps;
	bool operator==(const LiveKey& o) const { return memcmp(this, &o, sizeof(LiveKey)) == 0; }
};
static_assert(sizeof(LiveKey) == 48, "LiveKey must not have padding");
struct LiveKeyHash {
	size_t operator()(const LiveKey& k) const
	{
		const uint8_t* p = (const uint8_t*)&k;
		uint64_t h = 1469598103934665603ull;
		for (size_t i = 0; i < sizeof(LiveKey); i++)
			h = (h ^ p[i]) * 1099511628211ull;
		return (size_t)h;
	}
};
struct LiveEntry { uint32_t key, draws, instances; };
struct LiveFrame {
	uint32_t frame = 0;
	uint64_t t_ms = 0;
	uint32_t draws = 0, game_draws = 0, volatile_skips = 0;
	double   cost_ms = 0;
	uint64_t arena_bytes = 0;
	std::vector<LiveEntry> items;
};
static LiveConfig g_live;                         // guarded by g_lock
static volatile LONG g_live_on = 0;
static uint64_t g_live_generation = 1;           // guarded by g_lock
static std::deque<LiveFrame> g_live_ring;         // guarded by g_lock
static std::vector<LiveKey> g_live_keys;          // guarded by g_lock
static std::unordered_map<LiveKey, uint32_t, LiveKeyHash> g_live_key_ids;   // guarded by g_lock
static uint32_t g_live_key_gen = 0;               // changes when key numbers are reassigned (g_lock)
static size_t g_live_compact_at = 1u << 18;       // key count at which unused keys are dropped (g_lock)

static void live_clear_locked()
{
	g_live_ring.clear();
	g_live_keys.clear();
	g_live_keys.shrink_to_fit();
	g_live_key_ids.clear();
	g_live_key_gen++;
	g_live_compact_at = 1u << 18;
}

// Trigger (see "trigger" below). What the client reads is guarded by g_lock; the rest belongs to
// the render thread, which takes the client's commands over at frame boundaries (g_trig_cmd).
enum { TRIG_OFF = 0, TRIG_ARMING, TRIG_BASELINE, TRIG_ARMED, TRIG_FIRED, TRIG_RECORDING, TRIG_DONE, TRIG_EXPIRED };
static const char* k_trig_state[] = { "off", "arming", "baseline", "armed", "fired", "recording", "done", "expired" };
enum { TCMD_NONE = 0, TCMD_ARM, TCMD_CANCEL };
struct TrigFire {
	uint32_t frame = 0, draw = 0, count = 0;
	uint8_t  type = 0, origin = 0;
	bool     late = false;           // seen only once the frame's index data had been read back
	std::string what;                // "ib", "shader" or "new pair"
	uint32_t ib = 0;
	uint64_t shader = 0, vs = 0, ps = 0;
};
static int g_trig_state = TRIG_OFF;
static int g_trig_cmd = TCMD_NONE;
static TriggerSpec g_trig_next;                  // handed over with TCMD_ARM
static uint32_t g_trig_id = 0;                   // one per arming
static uint32_t g_trig_armed_frame = 0, g_trig_watched = 0, g_trig_capture_frame = 0;
static uint64_t g_trig_armed_ms = 0, g_trig_fired_ms = 0;
static TrigFire g_trig_fire;
static std::shared_ptr<Capture> g_trig_result;
static HANDLE g_trig_event = nullptr;            // set when it stops waiting: done, expired or cancelled
static volatile LONG g_trig_watch = 0;           // the draws being issued are compared

static TriggerSpec t_spec;                       // render thread from here on
static uint32_t t_id = 0;
static int t_phase = TRIG_OFF;                   // BASELINE, ARMED, FIRED, RECORDING or OFF
static std::unordered_set<uint32_t> t_ib;
static std::unordered_set<uint64_t> t_shader;
static std::unordered_set<uint64_t> t_pairs;     // vertex/pixel shader pairs drawn during the baseline
static uint32_t t_baseline_left = 0, t_fire_frame = 0;

// Recording (see record.h). The client's command and the status are guarded by g_lock; the rest
// belongs to the render thread.
enum { RCMD_NONE = 0, RCMD_START, RCMD_STOP };
static int g_rec_cmd = RCMD_NONE;
static volatile LONG g_record_on = 0;
static volatile LONG g_client_on = 0;            // a client is connected; buffer writes are tracked for it
                                                 // and for a recording
struct RecStatus {
	uint32_t id = 0, start_frame = 0, stop_frame = 0, frames = 0, keyframes = 0, lines = 0, runs = 0, news = 0;
	uint64_t start_ms = 0, stop_ms = 0;
	std::string started_by, stopped_by, last_keyframe;
	bool keyframes_stopped = false;
};
static RecStatus g_rs;
struct RRun {                                    // the run a line is in, or was in last
	uint32_t first = 0, last = 0, frames = 0, max_draws = 0, max_inst = 0;
	uint64_t draws = 0;
	uint32_t seen = 0, cur_draws = 0, cur_inst = 0;   // the frame being added and the line's counts in it
	bool     open = false;
};
static uint32_t r_id = 0, r_start_frame = 0, r_news_frame = 0, r_news = 0, r_keyframes = 0, r_runs_done = 0;
static uint32_t r_frames = 0;
static double r_t0 = 0, r_last_kf = 0, r_last_checkpoint = 0;
static bool r_first_kf = false, r_kf_stopped = false, r_key_down = false;
static std::string r_news_what, r_kf_what;
static const char* r_kf_reason = "";
static std::unordered_map<LiveKey, uint32_t, LiveKeyHash> r_ids;
static std::vector<RRun> r_runs;                 // by line number
static std::vector<uint32_t> r_open;             // the lines whose run is open
static std::unordered_set<uint64_t> r_pairs;     // vertex/pixel shader pairs the game drew since the start
static std::string r_lines_out, r_runs_out;      // handed to the writer about once a second
static std::vector<uint8_t> r_frames_out;
static const uint32_t k_rec_baseline = 30;       // frames at the start whose shader pairs are not news

static void trig_cancel_locked()
{
	if (g_trig_state == TRIG_OFF)
		return;
	g_trig_cmd = TCMD_CANCEL;
	g_trig_state = TRIG_OFF;
	g_trig_result.reset();
	InterlockedExchange(&g_trig_watch, 0);
	SetEvent(g_trig_event);
}

static volatile LONG g_client_gen = 0;           // bumped whenever a client (re)connects
static LONG g_seen_client_gen = 0;
static uint32_t g_track_gen = 1;

// Region hashes kept from frame to frame. The key is an address, and addresses
// are handed out again: a mod's buffers are destroyed and created anew at every
// reload, with the same sizes, and some of them where an earlier buffer was. An
// entry stays until its key is seen again, so that buffer need not be the one
// just destroyed. A new buffer that was never written after its creation has
// the same write epoch as the old one had, so the entry also names the object
// it was computed for.
struct RVal { uint32_t hash; uint64_t epoch; uint32_t gen; uint64_t serial; };
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
	g_trig_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
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
	EnterCriticalSection(&g_lock);
	InterlockedExchange(&g_client_on, 1);
	InterlockedIncrement(&g_client_gen);
	InterlockedExchange(&g_tracking, 1);
	LeaveCriticalSection(&g_lock);
}

void cap_client_disconnected()
{
	EnterCriticalSection(&g_lock);
	InterlockedExchange(&g_live_on, 0);
	g_live_generation++;
	live_clear_locked();
	trig_cancel_locked();                // nobody is left to collect what it would capture
	InterlockedExchange(&g_client_on, 0);
	InterlockedExchange(&g_tracking, g_record_on ? 1 : 0);   // a recording goes on without its client
	InterlockedIncrement(&g_client_gen);
	LeaveCriticalSection(&g_lock);
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
	memset(uavs, 0, sizeof(uavs));
	OCTX(OMGetRenderTargetsAndUnorderedAccessViews)(rctx(), 0, nullptr, nullptr, 0, n, uavs);
	for (UINT i = 0; i < n; i++) {
		if (!uavs[i]) continue;
		g_om_uav[i] = view_resource((ID3D11View*)uavs[i]);
		ID3D11UnorderedAccessView_Release(uavs[i]);
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

void cap_view_written(ID3D11View* view)
{
	void* r = view_resource(view);
	if (r)
		reg_buffer_written(r, 0, 0xffffffffu);
}

void cap_outputs_changed(bool compute);

void cap_state_swapped()
{
	if (g_tracking)
		resync_gpu_writers();
	cap_outputs_changed(false);
	cap_outputs_changed(true);
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
		cr.create_frame = ri.create_frame;
		cr.serial = ri.serial;
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

// The description is read at every use and compared with the one on record. The
// address only finds the candidate: an object that is destroyed while the frame
// is recorded can have a successor that says something else at the same address.
static int32_t state_id(Capture* c, void* obj, uint8_t kind)
{
	CapState s;
	memset(&s, 0, sizeof(s));
	s.ptr = obj;
	s.kind = kind;
	switch (kind) {
	case SK_BLEND:   ID3D11BlendState_GetDesc((ID3D11BlendState*)obj, &s.d.blend); break;
	case SK_DEPTH:   ID3D11DepthStencilState_GetDesc((ID3D11DepthStencilState*)obj, &s.d.depth); break;
	case SK_RASTER:  ID3D11RasterizerState_GetDesc((ID3D11RasterizerState*)obj, &s.d.raster); break;
	case SK_SAMPLER: ID3D11SamplerState_GetDesc((ID3D11SamplerState*)obj, &s.d.sampler); break;
	}
	auto it = c->state_ids.find(obj);
	if (it != c->state_ids.end()) {
		const CapState& known = c->states[it->second];
		if (known.kind == kind && !memcmp(&known.d, &s.d, sizeof(s.d)))
			return it->second;
	}
	int32_t id = (int32_t)c->states.size();
	c->states.push_back(s);
	c->state_ids[obj] = id;
	return id;
}

static bool same_view(const CapView& a, const CapView& b)
{
	return a.res == b.res && a.kind == b.kind && a.format == b.format && a.dim == b.dim && a.flags == b.flags
		&& !memcmp(a.p, b.p, sizeof(a.p));
}

// What part of its resource a view shows, and in which format. Compared by
// content like the state objects, for the same reason.
static int32_t view_desc_id(Capture* c, ID3D11View* v, uint8_t kind, int32_t rid)
{
	if (!v) return -1;
	CapView cv;
	memset(&cv, 0, sizeof(cv));
	cv.ptr = v;
	cv.res = rid;
	cv.kind = kind;
	switch (kind) {
	case VK_SRV: {
		D3D11_SHADER_RESOURCE_VIEW_DESC d;
		ID3D11ShaderResourceView_GetDesc((ID3D11ShaderResourceView*)v, &d);
		cv.format = d.Format; cv.dim = d.ViewDimension;
		switch (d.ViewDimension) {
		case D3D11_SRV_DIMENSION_BUFFER:           cv.p[0] = d.Buffer.FirstElement; cv.p[1] = d.Buffer.NumElements; break;
		case D3D11_SRV_DIMENSION_BUFFEREX:         cv.p[0] = d.BufferEx.FirstElement; cv.p[1] = d.BufferEx.NumElements; cv.flags = d.BufferEx.Flags; break;
		case D3D11_SRV_DIMENSION_TEXTURE1D:        cv.p[0] = d.Texture1D.MostDetailedMip; cv.p[1] = d.Texture1D.MipLevels; break;
		case D3D11_SRV_DIMENSION_TEXTURE1DARRAY:   cv.p[0] = d.Texture1DArray.MostDetailedMip; cv.p[1] = d.Texture1DArray.MipLevels;
		                                           cv.p[2] = d.Texture1DArray.FirstArraySlice; cv.p[3] = d.Texture1DArray.ArraySize; break;
		case D3D11_SRV_DIMENSION_TEXTURE2D:        cv.p[0] = d.Texture2D.MostDetailedMip; cv.p[1] = d.Texture2D.MipLevels; break;
		case D3D11_SRV_DIMENSION_TEXTURE2DARRAY:   cv.p[0] = d.Texture2DArray.MostDetailedMip; cv.p[1] = d.Texture2DArray.MipLevels;
		                                           cv.p[2] = d.Texture2DArray.FirstArraySlice; cv.p[3] = d.Texture2DArray.ArraySize; break;
		case D3D11_SRV_DIMENSION_TEXTURE2DMSARRAY: cv.p[2] = d.Texture2DMSArray.FirstArraySlice; cv.p[3] = d.Texture2DMSArray.ArraySize; break;
		case D3D11_SRV_DIMENSION_TEXTURE3D:        cv.p[0] = d.Texture3D.MostDetailedMip; cv.p[1] = d.Texture3D.MipLevels; break;
		case D3D11_SRV_DIMENSION_TEXTURECUBE:      cv.p[0] = d.TextureCube.MostDetailedMip; cv.p[1] = d.TextureCube.MipLevels; break;
		case D3D11_SRV_DIMENSION_TEXTURECUBEARRAY: cv.p[0] = d.TextureCubeArray.MostDetailedMip; cv.p[1] = d.TextureCubeArray.MipLevels;
		                                           cv.p[2] = d.TextureCubeArray.First2DArrayFace; cv.p[3] = d.TextureCubeArray.NumCubes; break;
		default: break;
		}
		break;
	}
	case VK_RTV: {
		D3D11_RENDER_TARGET_VIEW_DESC d;
		ID3D11RenderTargetView_GetDesc((ID3D11RenderTargetView*)v, &d);
		cv.format = d.Format; cv.dim = d.ViewDimension;
		switch (d.ViewDimension) {
		case D3D11_RTV_DIMENSION_BUFFER:           cv.p[0] = d.Buffer.FirstElement; cv.p[1] = d.Buffer.NumElements; break;
		case D3D11_RTV_DIMENSION_TEXTURE1D:        cv.p[0] = d.Texture1D.MipSlice; break;
		case D3D11_RTV_DIMENSION_TEXTURE1DARRAY:   cv.p[0] = d.Texture1DArray.MipSlice; cv.p[2] = d.Texture1DArray.FirstArraySlice; cv.p[3] = d.Texture1DArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE2D:        cv.p[0] = d.Texture2D.MipSlice; break;
		case D3D11_RTV_DIMENSION_TEXTURE2DARRAY:   cv.p[0] = d.Texture2DArray.MipSlice; cv.p[2] = d.Texture2DArray.FirstArraySlice; cv.p[3] = d.Texture2DArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE2DMSARRAY: cv.p[2] = d.Texture2DMSArray.FirstArraySlice; cv.p[3] = d.Texture2DMSArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE3D:        cv.p[0] = d.Texture3D.MipSlice; cv.p[2] = d.Texture3D.FirstWSlice; cv.p[3] = d.Texture3D.WSize; break;
		default: break;
		}
		break;
	}
	case VK_DSV: {
		D3D11_DEPTH_STENCIL_VIEW_DESC d;
		ID3D11DepthStencilView_GetDesc((ID3D11DepthStencilView*)v, &d);
		cv.format = d.Format; cv.dim = d.ViewDimension; cv.flags = d.Flags;
		switch (d.ViewDimension) {
		case D3D11_DSV_DIMENSION_TEXTURE1D:        cv.p[0] = d.Texture1D.MipSlice; break;
		case D3D11_DSV_DIMENSION_TEXTURE1DARRAY:   cv.p[0] = d.Texture1DArray.MipSlice; cv.p[2] = d.Texture1DArray.FirstArraySlice; cv.p[3] = d.Texture1DArray.ArraySize; break;
		case D3D11_DSV_DIMENSION_TEXTURE2D:        cv.p[0] = d.Texture2D.MipSlice; break;
		case D3D11_DSV_DIMENSION_TEXTURE2DARRAY:   cv.p[0] = d.Texture2DArray.MipSlice; cv.p[2] = d.Texture2DArray.FirstArraySlice; cv.p[3] = d.Texture2DArray.ArraySize; break;
		case D3D11_DSV_DIMENSION_TEXTURE2DMSARRAY: cv.p[2] = d.Texture2DMSArray.FirstArraySlice; cv.p[3] = d.Texture2DMSArray.ArraySize; break;
		default: break;
		}
		break;
	}
	case VK_UAV: {
		D3D11_UNORDERED_ACCESS_VIEW_DESC d;
		ID3D11UnorderedAccessView_GetDesc((ID3D11UnorderedAccessView*)v, &d);
		cv.format = d.Format; cv.dim = d.ViewDimension;
		switch (d.ViewDimension) {
		case D3D11_UAV_DIMENSION_BUFFER:           cv.p[0] = d.Buffer.FirstElement; cv.p[1] = d.Buffer.NumElements; cv.flags = d.Buffer.Flags; break;
		case D3D11_UAV_DIMENSION_TEXTURE1D:        cv.p[0] = d.Texture1D.MipSlice; break;
		case D3D11_UAV_DIMENSION_TEXTURE1DARRAY:   cv.p[0] = d.Texture1DArray.MipSlice; cv.p[2] = d.Texture1DArray.FirstArraySlice; cv.p[3] = d.Texture1DArray.ArraySize; break;
		case D3D11_UAV_DIMENSION_TEXTURE2D:        cv.p[0] = d.Texture2D.MipSlice; break;
		case D3D11_UAV_DIMENSION_TEXTURE2DARRAY:   cv.p[0] = d.Texture2DArray.MipSlice; cv.p[2] = d.Texture2DArray.FirstArraySlice; cv.p[3] = d.Texture2DArray.ArraySize; break;
		case D3D11_UAV_DIMENSION_TEXTURE3D:        cv.p[0] = d.Texture3D.MipSlice; cv.p[2] = d.Texture3D.FirstWSlice; cv.p[3] = d.Texture3D.WSize; break;
		default: break;
		}
		break;
	}
	}
	auto it = c->view_ids.find(v);
	if (it != c->view_ids.end() && same_view(c->view_descs[it->second], cv))
		return it->second;
	int32_t id = (int32_t)c->view_descs.size();
	c->view_descs.push_back(cv);
	c->view_ids[v] = id;
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
			if (p != g_rcache.end() && p->second.gen == g_track_gen && p->second.serial == cr.serial
			    && hist_intact(hist, off, size, p->second.epoch)) {
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
	c->pending.push_back({ 0, idx, chunk, at, size, key, epoch, g_track_gen, cr.serial });
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
	c->pending.push_back({ 1, idx, chunk, at, size, key, epoch, g_track_gen, cr.serial });
	if (epoch)
		c->frame_snaps[key] = { idx, epoch };
	return idx;
}

// ================================================================ contents
// What a call read and wrote, as it was when the call was issued, for as long
// as the capture is held.
//
// Recording a frame copies nothing for this: a draw only notes which version
// of each resource it saw. Whatever is about to change a resource (a map, an
// update, a copy, a clear, a draw or a dispatch that has it bound for writing)
// passes one of the hooks first, and there the version that is still wanted is
// set aside: of a buffer only the bytes about to be overwritten, of a texture
// the subresource. A resource nothing writes to again is read where it is. So
// a static mesh or texture costs nothing however many draws use it, and
// something that changes is copied once for every version that was used.
static Capture* g_bar = nullptr;                       // the capture whose contents are being kept
// What has been set aside for it so far, for the status: written by the render
// thread once a frame, read by the pipe thread, which must not touch the capture.
static volatile LONG64 g_kept_bytes = 0;
static volatile LONG g_kept_copies = 0, g_kept_lost = 0, g_kept_frame = 0;

static void cont_publish(const Capture* c)
{
	InterlockedExchange64(&g_kept_bytes, c ? (LONG64)c->ct_bytes : 0);
	InterlockedExchange(&g_kept_copies, c ? (LONG)c->ct_copies : 0);
	InterlockedExchange(&g_kept_lost, c ? (LONG)c->ct_lost : 0);
	InterlockedExchange(&g_kept_frame, c ? (LONG)c->frame : 0);
}
static std::unordered_map<void*, uint32_t> g_mapped;   // what the game has mapped right now

static bool memory_allows(uint64_t bytes)
{
	MEMORYSTATUSEX m;
	memset(&m, 0, sizeof(m));
	m.dwLength = sizeof(m);
	if (!GlobalMemoryStatusEx(&m))
		return true;
	// The game comes first: never take the machine below 2 GiB of free memory.
	const uint64_t reserve = 2048ull << 20;
	return m.ullAvailPhys > bytes + reserve && m.ullAvailPageFile > bytes + reserve;
}

static bool is_block_compressed(DXGI_FORMAT f)
{
	return (f >= DXGI_FORMAT_BC1_TYPELESS && f <= DXGI_FORMAT_BC5_SNORM) ||
	       (f >= DXGI_FORMAT_BC6H_TYPELESS && f <= DXGI_FORMAT_BC7_UNORM_SRGB);
}

static bool is_typeless(DXGI_FORMAT f)
{
	switch (f) {
	case DXGI_FORMAT_R32G32B32A32_TYPELESS: case DXGI_FORMAT_R32G32B32_TYPELESS: case DXGI_FORMAT_R16G16B16A16_TYPELESS:
	case DXGI_FORMAT_R32G32_TYPELESS: case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_R10G10B10A2_TYPELESS:
	case DXGI_FORMAT_R8G8B8A8_TYPELESS: case DXGI_FORMAT_R16G16_TYPELESS: case DXGI_FORMAT_R32_TYPELESS:
	case DXGI_FORMAT_R24G8_TYPELESS: case DXGI_FORMAT_R8G8_TYPELESS: case DXGI_FORMAT_R16_TYPELESS:
	case DXGI_FORMAT_R8_TYPELESS: case DXGI_FORMAT_BC1_TYPELESS: case DXGI_FORMAT_BC2_TYPELESS:
	case DXGI_FORMAT_BC3_TYPELESS: case DXGI_FORMAT_BC4_TYPELESS: case DXGI_FORMAT_BC5_TYPELESS:
	case DXGI_FORMAT_B8G8R8A8_TYPELESS: case DXGI_FORMAT_B8G8R8X8_TYPELESS: case DXGI_FORMAT_BC6H_TYPELESS:
	case DXGI_FORMAT_BC7_TYPELESS:
		return true;
	default:
		return false;
	}
}

static bool is_depth_format(DXGI_FORMAT f)
{
	switch (f) {
	case DXGI_FORMAT_R32G8X24_TYPELESS: case DXGI_FORMAT_D32_FLOAT_S8X24_UINT: case DXGI_FORMAT_R32_FLOAT_X8X24_TYPELESS:
	case DXGI_FORMAT_X32_TYPELESS_G8X24_UINT: case DXGI_FORMAT_D32_FLOAT: case DXGI_FORMAT_R24G8_TYPELESS:
	case DXGI_FORMAT_D24_UNORM_S8_UINT: case DXGI_FORMAT_R24_UNORM_X8_TYPELESS: case DXGI_FORMAT_X24_TYPELESS_G8_UINT:
	case DXGI_FORMAT_D16_UNORM:
		return true;
	default:
		return false;
	}
}

static uint64_t surface_bytes(uint32_t w, uint32_t h, uint32_t d, DXGI_FORMAT f)
{
	size_t n = 0, row = 0, rows = 0;
	dxgi_surface_info(w, h, f, &n, &row, &rows);
	return (uint64_t)n * d;
}

static uint32_t mips_of(const CapRes& cr)
{
	uint32_t n = cr.dim == D3D11_RESOURCE_DIMENSION_TEXTURE2D ? cr.live.t2.MipLevels
	           : cr.dim == D3D11_RESOURCE_DIMENSION_TEXTURE1D ? cr.live.t1.MipLevels
	           : cr.dim == D3D11_RESOURCE_DIMENSION_TEXTURE3D ? cr.live.t3.MipLevels : 1;
	return n ? n : 1;
}

struct Staged {
	ID3D11Resource* tex = nullptr;
	uint8_t  dim = 0;
	bool     resolved = false;
	DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
	uint32_t w = 0, h = 0, d = 1;
	uint64_t bytes = 0;
};

// Queues a copy of one subresource, as it is now, into a staging texture of its
// own. Returns 0, or the CS_* reason why there is none. hint: the format of the
// view the texture was seen through, which a typeless multisampled texture needs
// to be resolved. limit: bytes that may still be spent, 0 for any number.
static int stage_subresource(ID3D11Resource* r, const AnyDesc& live, uint8_t dim, uint32_t mip, uint32_t slice,
                             DXGI_FORMAT hint, uint64_t limit, Staged* out)
{
	ID3D11Device* dev = rdev();
	ID3D11DeviceContext* ctx = rctx();
	out->dim = dim;
	if (g_mapped.count(r))
		return CS_MAPPED;                // the runtime would drop the copy without a word

	if (dim == D3D11_RESOURCE_DIMENSION_TEXTURE1D) {
		// XXMI's IniParams live in a 1D texture, which makes this worth having.
		const D3D11_TEXTURE1D_DESC& d = live.t1;
		if (mip >= d.MipLevels || slice >= d.ArraySize) return CS_FAILED;
		out->w = std::max(d.Width >> mip, (UINT)1);
		out->h = 1;
		out->format = d.Format;
		out->bytes = surface_bytes(out->w, 1, 1, d.Format);
		if (!out->bytes) return CS_UNSUPPORTED;
		if (limit && out->bytes > limit) return CS_BUDGET;
		D3D11_TEXTURE1D_DESC s;
		memset(&s, 0, sizeof(s));
		s.Width = out->w;
		s.MipLevels = 1;
		s.ArraySize = 1;
		s.Format = d.Format;
		s.Usage = D3D11_USAGE_STAGING;
		s.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
		ID3D11Texture1D* st = nullptr;
		if (FAILED(ODEV(CreateTexture1D)(dev, &s, nullptr, &st)) || !st) return CS_FAILED;
		OCTX(CopySubresourceRegion)(ctx, (ID3D11Resource*)st, 0, 0, 0, 0, r, mip + slice * d.MipLevels, nullptr);
		out->tex = (ID3D11Resource*)st;
		return 0;
	}

	if (dim == D3D11_RESOURCE_DIMENSION_TEXTURE3D) {
		const D3D11_TEXTURE3D_DESC& d = live.t3;
		if (mip >= d.MipLevels) return CS_FAILED;
		out->w = std::max(d.Width >> mip, (UINT)1);
		out->h = std::max(d.Height >> mip, (UINT)1);
		out->d = std::max(d.Depth >> mip, (UINT)1);
		out->format = d.Format;
		out->bytes = surface_bytes(out->w, out->h, out->d, d.Format);
		if (!out->bytes) return CS_UNSUPPORTED;
		if (limit && out->bytes > limit) return CS_BUDGET;
		if (out->bytes >= (1u << 20) && !memory_allows(out->bytes)) return CS_MEMORY;
		D3D11_TEXTURE3D_DESC s;
		memset(&s, 0, sizeof(s));
		s.Width = out->w;
		s.Height = out->h;
		if (is_block_compressed(d.Format)) {
			s.Width = (out->w + 3) & ~3u;
			s.Height = (out->h + 3) & ~3u;
		}
		s.Depth = out->d;
		s.MipLevels = 1;
		s.Format = d.Format;
		s.Usage = D3D11_USAGE_STAGING;
		s.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
		ID3D11Texture3D* st = nullptr;
		if (FAILED(ODEV(CreateTexture3D)(dev, &s, nullptr, &st)) || !st) return CS_FAILED;
		OCTX(CopySubresourceRegion)(ctx, (ID3D11Resource*)st, 0, 0, 0, 0, r, mip, nullptr);
		out->tex = (ID3D11Resource*)st;
		return 0;
	}

	if (dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D)
		return CS_UNSUPPORTED;
	const D3D11_TEXTURE2D_DESC& d = live.t2;
	if (mip >= d.MipLevels || slice >= d.ArraySize) return CS_FAILED;
	out->w = std::max(d.Width >> mip, (UINT)1);
	out->h = std::max(d.Height >> mip, (UINT)1);
	DXGI_FORMAT fmt = d.Format;
	const bool multisampled = d.SampleDesc.Count > 1;
	if (multisampled) {
		// Samples can only be read back resolved, and depth cannot be resolved.
		if ((d.BindFlags & D3D11_BIND_DEPTH_STENCIL) || is_depth_format(fmt)) return CS_UNSUPPORTED;
		if (is_typeless(fmt)) fmt = hint;
		UINT support = 0;
		if (fmt == DXGI_FORMAT_UNKNOWN || is_typeless(fmt) || FAILED(ODEV(CheckFormatSupport)(dev, fmt, &support))
		    || !(support & D3D11_FORMAT_SUPPORT_MULTISAMPLE_RESOLVE))
			return CS_UNSUPPORTED;
	}
	out->format = fmt;
	out->bytes = surface_bytes(out->w, out->h, 1, fmt);
	if (!out->bytes) return CS_UNSUPPORTED;
	if (limit && out->bytes > limit) return CS_BUDGET;
	if (out->bytes >= (1u << 20) && !memory_allows(out->bytes)) return CS_MEMORY;

	D3D11_TEXTURE2D_DESC s;
	memset(&s, 0, sizeof(s));
	s.Width = out->w;
	s.Height = out->h;
	if (is_block_compressed(fmt)) {
		s.Width = (out->w + 3) & ~3u;
		s.Height = (out->h + 3) & ~3u;
	}
	s.MipLevels = 1;
	s.ArraySize = 1;
	s.Format = fmt;
	s.SampleDesc.Count = 1;
	s.Usage = D3D11_USAGE_STAGING;
	s.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
	ID3D11Texture2D* st = nullptr;
	if (FAILED(ODEV(CreateTexture2D)(dev, &s, nullptr, &st)) || !st) return CS_FAILED;
	const UINT sub = mip + slice * d.MipLevels;
	if (multisampled) {
		D3D11_TEXTURE2D_DESC t = s;
		t.Usage = D3D11_USAGE_DEFAULT;
		t.CPUAccessFlags = 0;
		t.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		ID3D11Texture2D* tmp = nullptr;
		if (FAILED(ODEV(CreateTexture2D)(dev, &t, nullptr, &tmp)) || !tmp) {
			ID3D11Texture2D_Release(st);
			return CS_FAILED;
		}
		OCTX(ResolveSubresource)(ctx, (ID3D11Resource*)tmp, 0, r, sub, fmt);
		OCTX(CopyResource)(ctx, (ID3D11Resource*)st, (ID3D11Resource*)tmp);
		ID3D11Texture2D_Release(tmp);
		out->resolved = true;
	} else {
		OCTX(CopySubresourceRegion)(ctx, (ID3D11Resource*)st, 0, 0, 0, 0, r, sub, nullptr);
	}
	out->tex = (ID3D11Resource*)st;
	return 0;
}

// ---- buffers
// Sets aside bytes of a buffer in a staging chunk the capture owns.
static int cont_store(Capture* c, ID3D11Buffer* src, uint32_t off, uint32_t size, uint32_t* chunk, uint32_t* at)
{
	if (c->ct_bytes + size > c->rq.ct_budget)
		return CS_BUDGET;
	if (g_mapped.count(src))
		return CS_MAPPED;
	if (c->cchunks.empty() || c->cchunks.back().cap - c->cchunks.back().used < size) {
		const uint32_t want = size > (4u << 20) ? (size + 0xFFFFu) & ~0xFFFFu : 4u << 20;
		if (!memory_allows(want))
			return CS_MEMORY;
		ID3D11Buffer* b = create_staging(want);
		if (!b)
			return CS_FAILED;
		c->cchunks.push_back({ b, want, 0 });
	}
	ContChunk& ch = c->cchunks.back();
	D3D11_BOX box = { off, 0, 0, off + size, 1, 1 };
	OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)ch.buf, 0, ch.used, 0, 0, (ID3D11Resource*)src, 0, &box);
	*chunk = (uint32_t)c->cchunks.size() - 1;
	*at = ch.used;
	ch.used += size;
	c->ct_bytes += size;
	c->ct_copies++;
	return 0;
}

// The version of a buffer a call sees.
static int32_t cont_buffer(Capture* c, ID3D11Buffer* buf, int32_t rid)
{
	if (rid < 0)
		return -1;
	ContTrack& t = c->tracks[buf];
	if (t.rid < 0) {
		t.rid = rid;
		t.buffer = true;
	}
	if (t.cur >= 0 && c->conts[t.cur].ver == t.ver)
		return t.cur;
	ContSnap s;
	memset(&s, 0, sizeof(s));
	s.res = rid;
	s.ver = t.ver;
	t.cur = (int32_t)c->conts.size();
	t.sealed = false;
	t.since_ref = 0;
	c->conts.push_back(s);
	return t.cur;
}

// [off, off+size) of a buffer is about to be overwritten.
static void cont_buffer_write(Capture* c, ContTrack& t, ID3D11Buffer* buf, uint32_t off, uint32_t size)
{
	const uint32_t bw = c->res[t.rid].live.buf.ByteWidth;
	if (off >= bw)
		return;
	if (size > bw - off)
		size = bw - off;
	if (!size)
		return;
	if (!t.sealed) {
		// Many small pieces come to more than the buffer itself: keep all of it once instead.
		if (t.since_ref + size > bw) {
			off = 0;
			size = bw;
		}
		uint32_t chunk = 0, at = 0;
		const int why = cont_store(c, buf, off, size, &chunk, &at);
		if (!why) {
			t.patches.push_back({ t.ver, off, size, chunk, at });
			t.since_ref += size;
			if (size == bw)
				t.sealed = true;
		} else {
			t.lost = true;
			t.lost_ver = t.ver;
			t.lost_why = (uint8_t)why;
			t.sealed = true;             // the versions so far are beyond help; a new reference starts over
			c->ct_lost++;
		}
	}
	t.ver++;
}

// ---- textures
// One subresource of a texture as a call sees it.
static int32_t cont_texture(Capture* c, ID3D11Resource* r, int32_t rid, uint32_t mip, uint32_t slice, DXGI_FORMAT hint)
{
	if (rid < 0)
		return -1;
	const uint32_t sub = mip + slice * mips_of(c->res[rid]);
	ContTrack& t = c->tracks[r];
	if (t.rid < 0)
		t.rid = rid;
	for (const ContPending& p : t.pending)
		if (p.sub == sub)
			return p.snap;
	ContSnap s;
	memset(&s, 0, sizeof(s));
	s.res = rid;
	s.tex = 1;
	s.state = CS_LIVE;
	s.mip = mip;
	s.slice = slice;
	s.hint = hint;
	const int32_t idx = (int32_t)c->conts.size();
	c->conts.push_back(s);
	t.pending.push_back({ sub, idx });
	return idx;
}

// Subresources of a texture are about to be written: those of one mip in slices
// [first, first+count), or all of them when count is 0.
static void cont_texture_write(Capture* c, ContTrack& t, ID3D11Resource* r, uint32_t mip, uint32_t first, uint32_t count)
{
	if (t.pending.empty())
		return;
	const CapRes& cr = c->res[t.rid];
	const uint32_t mips = mips_of(cr);
	for (size_t i = t.pending.size(); i-- > 0;) {
		const uint32_t sub = t.pending[i].sub;
		if (count && (sub % mips != mip || sub / mips < first || sub / mips - first >= count))
			continue;
		ContSnap& s = c->conts[t.pending[i].snap];
		const uint64_t left = c->rq.ct_budget > c->ct_bytes ? c->rq.ct_budget - c->ct_bytes : 0;
		Staged st;
		const int why = left ? stage_subresource(r, cr.live, cr.dim, s.mip, s.slice, s.hint, left, &st) : CS_BUDGET;
		if (!why) {
			s.copy = st.tex;
			s.state = CS_COPIED;
			s.dim = st.dim;
			s.resolved = st.resolved;
			s.w = st.w; s.h = st.h; s.d = st.d;
			s.format = st.format;
			s.bytes = st.bytes;
			c->ct_bytes += st.bytes;
			c->ct_copies++;
		} else {
			s.state = (uint8_t)why;
			c->ct_lost++;
		}
		t.pending.erase(t.pending.begin() + i);
	}
}

// What a view shows of its resource, as a call sees it.
static int32_t cont_of_view(Capture* c, int32_t rid, int32_t view_id, bool buffers, bool textures)
{
	if (rid < 0)
		return -1;
	const CapRes& cr = c->res[rid];
	if (cr.dim == D3D11_RESOURCE_DIMENSION_BUFFER)
		return buffers ? cont_buffer(c, (ID3D11Buffer*)cr.ptr, rid) : -1;
	if (!textures || view_id < 0 || cr.dim == D3D11_RESOURCE_DIMENSION_UNKNOWN)
		return -1;
	const CapView& cv = c->view_descs[view_id];
	const uint32_t slice = cr.dim == D3D11_RESOURCE_DIMENSION_TEXTURE3D ? 0 : cv.p[2];
	return cont_texture(c, (ID3D11Resource*)cr.ptr, rid, cv.p[0], slice, (DXGI_FORMAT)cv.format);
}

// Which subresources a view writes to: one mip, array slices [first, first+count).
// A shader resource view (GenerateMips) answers with count 0: all of them.
static void view_span(ID3D11View* v, int kind, uint32_t* mip, uint32_t* first, uint32_t* count)
{
	*mip = 0; *first = 0; *count = 1;
	switch (kind) {
	case VK_RTV: {
		D3D11_RENDER_TARGET_VIEW_DESC d;
		ID3D11RenderTargetView_GetDesc((ID3D11RenderTargetView*)v, &d);
		switch (d.ViewDimension) {
		case D3D11_RTV_DIMENSION_TEXTURE1D:        *mip = d.Texture1D.MipSlice; break;
		case D3D11_RTV_DIMENSION_TEXTURE1DARRAY:   *mip = d.Texture1DArray.MipSlice; *first = d.Texture1DArray.FirstArraySlice; *count = d.Texture1DArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE2D:        *mip = d.Texture2D.MipSlice; break;
		case D3D11_RTV_DIMENSION_TEXTURE2DARRAY:   *mip = d.Texture2DArray.MipSlice; *first = d.Texture2DArray.FirstArraySlice; *count = d.Texture2DArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE2DMSARRAY: *first = d.Texture2DMSArray.FirstArraySlice; *count = d.Texture2DMSArray.ArraySize; break;
		case D3D11_RTV_DIMENSION_TEXTURE3D:        *mip = d.Texture3D.MipSlice; break;
		default: break;
		}
		break;
	}
	case VK_DSV: {
		D3D11_DEPTH_STENCIL_VIEW_DESC d;
		ID3D11DepthStencilView_GetDesc((ID3D11DepthStencilView*)v, &d);
		switch (d.ViewDimension) {
		case D3D11_DSV_DIMENSION_TEXTURE1D:        *mip = d.Texture1D.MipSlice; break;
		case D3D11_DSV_DIMENSION_TEXTURE1DARRAY:   *mip = d.Texture1DArray.MipSlice; *first = d.Texture1DArray.FirstArraySlice; *count = d.Texture1DArray.ArraySize; break;
		case D3D11_DSV_DIMENSION_TEXTURE2D:        *mip = d.Texture2D.MipSlice; break;
		case D3D11_DSV_DIMENSION_TEXTURE2DARRAY:   *mip = d.Texture2DArray.MipSlice; *first = d.Texture2DArray.FirstArraySlice; *count = d.Texture2DArray.ArraySize; break;
		case D3D11_DSV_DIMENSION_TEXTURE2DMSARRAY: *first = d.Texture2DMSArray.FirstArraySlice; *count = d.Texture2DMSArray.ArraySize; break;
		default: break;
		}
		break;
	}
	case VK_UAV: {
		D3D11_UNORDERED_ACCESS_VIEW_DESC d;
		ID3D11UnorderedAccessView_GetDesc((ID3D11UnorderedAccessView*)v, &d);
		switch (d.ViewDimension) {
		case D3D11_UAV_DIMENSION_TEXTURE1D:        *mip = d.Texture1D.MipSlice; break;
		case D3D11_UAV_DIMENSION_TEXTURE1DARRAY:   *mip = d.Texture1DArray.MipSlice; *first = d.Texture1DArray.FirstArraySlice; *count = d.Texture1DArray.ArraySize; break;
		case D3D11_UAV_DIMENSION_TEXTURE2D:        *mip = d.Texture2D.MipSlice; break;
		case D3D11_UAV_DIMENSION_TEXTURE2DARRAY:   *mip = d.Texture2DArray.MipSlice; *first = d.Texture2DArray.FirstArraySlice; *count = d.Texture2DArray.ArraySize; break;
		case D3D11_UAV_DIMENSION_TEXTURE3D:        *mip = d.Texture3D.MipSlice; break;
		default: break;
		}
		break;
	}
	default:
		*count = 0;
		break;
	}
	if (!*count)
		*mip = *first = 0;
}

// Waits until the GPU has done everything queued so far. Only the strict mode
// needs it: a no-overwrite map hands out the memory itself, so what is written
// through it is seen by a copy that was queued before the map but runs after.
static void gpu_sync()
{
	D3D11_QUERY_DESC qd = { D3D11_QUERY_EVENT, 0 };
	ID3D11Query* q = nullptr;
	if (FAILED(ODEV(CreateQuery)(rdev(), &qd, &q)) || !q)
		return;
	OCTX(End)(rctx(), (ID3D11Asynchronous*)q);
	OCTX(Flush)(rctx());
	BOOL done = FALSE;
	const uint64_t t0 = now_ms();
	while (OCTX(GetData)(rctx(), (ID3D11Asynchronous*)q, &done, sizeof(done), 0) != S_OK || !done) {
		if (now_ms() - t0 > 250)
			break;                       // never hold the game up for long
		SwitchToThread();
	}
	ID3D11Query_Release(q);
}

// ---- the hooks' side
// Which of the resources a capture watches the game has mapped: a copy from a
// mapped resource is dropped by the runtime without a word.
void cap_mapped(ID3D11Resource* res, bool mapped)
{
	if (mapped) {
		if (g_bar && g_bar->tracks.count(res))
			g_mapped[res]++;
	} else if (!g_mapped.empty()) {
		auto it = g_mapped.find(res);
		if (it != g_mapped.end() && --it->second == 0)
			g_mapped.erase(it);
	}
}

void cap_before_write(ID3D11Resource* res, uint32_t sub, uint32_t off, uint32_t size, bool keeps_rest)
{
	Capture* c = g_bar;
	if (!c || !res)
		return;
	auto it = c->tracks.find(res);
	if (it == c->tracks.end())
		return;
	ContTrack& t = it->second;
	if (t.buffer) {
		// A writer that promises to leave alone what is in use changes nothing a call has seen.
		if (!keeps_rest) {
			cont_buffer_write(c, t, (ID3D11Buffer*)res, off, size);
		} else if (c->rq.ct_strict) {
			const bool copies = !t.sealed;
			cont_buffer_write(c, t, (ID3D11Buffer*)res, off, size);
			if (copies)
				gpu_sync();
		}
	} else if (sub == 0xffffffffu) {
		cont_texture_write(c, t, res, 0, 0, 0);
	} else {
		const uint32_t mips = mips_of(c->res[t.rid]);
		cont_texture_write(c, t, res, sub % mips, sub / mips, 1);
	}
}

void cap_before_view_write(ID3D11View* view, int kind)
{
	Capture* c = g_bar;
	if (!c || !view)
		return;
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(view, &r);
	if (!r)
		return;
	auto it = c->tracks.find(r);
	if (it != c->tracks.end()) {
		ContTrack& t = it->second;
		if (t.buffer) {
			cont_buffer_write(c, t, (ID3D11Buffer*)r, 0, 0xffffffffu);
		} else {
			uint32_t mip, first, count;
			view_span(view, kind, &mip, &first, &count);
			cont_texture_write(c, t, r, mip, first, count);
		}
	}
	ID3D11Resource_Release(r);
}

// What the pipeline has bound for writing. The bindings are read from the
// runtime when they have changed, not at every call.
struct BoundOut { void* res; uint32_t mip, first, count; };
static BoundOut g_om_out[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT + 1 + D3D11_1_UAV_SLOT_COUNT + D3D11_SO_BUFFER_SLOT_COUNT];
static BoundOut g_cs_out[D3D11_1_UAV_SLOT_COUNT];
static uint32_t g_om_out_n = 0, g_cs_out_n = 0;
static bool g_om_dirty = true, g_cs_dirty = true;

void cap_outputs_changed(bool compute)
{
	if (compute) g_cs_dirty = true;
	else g_om_dirty = true;
}

static void bound_add(BoundOut* list, uint32_t* n, ID3D11View* v, int kind)
{
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(v, &r);
	if (!r)
		return;
	BoundOut b;
	b.res = r;
	view_span(v, kind, &b.mip, &b.first, &b.count);
	list[(*n)++] = b;
	ID3D11Resource_Release(r);           // identity only: what is bound stays alive
}

static UINT uav_slots()
{
	return g_feature_level >= D3D_FEATURE_LEVEL_11_1 ? D3D11_1_UAV_SLOT_COUNT : D3D11_PS_CS_UAV_REGISTER_COUNT;
}

static void refresh_om_outputs()
{
	g_om_out_n = 0;
	ID3D11RenderTargetView* rtvs[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT];
	ID3D11DepthStencilView* dsv = nullptr;
	static ID3D11UnorderedAccessView* uavs[D3D11_1_UAV_SLOT_COUNT];
	memset(rtvs, 0, sizeof(rtvs));
	memset(uavs, 0, sizeof(uavs));
	const UINT n = uav_slots();
	OCTX(OMGetRenderTargetsAndUnorderedAccessViews)(rctx(), D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtvs, &dsv, 0, n, uavs);
	for (UINT i = 0; i < _countof(rtvs); i++) {
		if (!rtvs[i]) continue;
		bound_add(g_om_out, &g_om_out_n, (ID3D11View*)rtvs[i], VK_RTV);
		ID3D11RenderTargetView_Release(rtvs[i]);
	}
	if (dsv) {
		D3D11_DEPTH_STENCIL_VIEW_DESC d;
		ID3D11DepthStencilView_GetDesc(dsv, &d);
		const UINT read_only = D3D11_DSV_READ_ONLY_DEPTH | D3D11_DSV_READ_ONLY_STENCIL;
		if ((d.Flags & read_only) != read_only)
			bound_add(g_om_out, &g_om_out_n, (ID3D11View*)dsv, VK_DSV);
		ID3D11DepthStencilView_Release(dsv);
	}
	for (UINT i = 0; i < n; i++) {
		if (!uavs[i]) continue;
		bound_add(g_om_out, &g_om_out_n, (ID3D11View*)uavs[i], VK_UAV);
		ID3D11UnorderedAccessView_Release(uavs[i]);
	}
	ID3D11Buffer* so[D3D11_SO_BUFFER_SLOT_COUNT];
	memset(so, 0, sizeof(so));
	OCTX(SOGetTargets)(rctx(), D3D11_SO_BUFFER_SLOT_COUNT, so);
	for (UINT i = 0; i < D3D11_SO_BUFFER_SLOT_COUNT; i++) {
		if (!so[i]) continue;
		g_om_out[g_om_out_n++] = { so[i], 0, 0, 0 };
		ID3D11Buffer_Release(so[i]);
	}
	g_om_dirty = false;
}

static void refresh_cs_outputs()
{
	g_cs_out_n = 0;
	static ID3D11UnorderedAccessView* uavs[D3D11_1_UAV_SLOT_COUNT];
	memset(uavs, 0, sizeof(uavs));
	const UINT n = uav_slots();
	OCTX(CSGetUnorderedAccessViews)(rctx(), 0, n, uavs);
	for (UINT i = 0; i < n; i++) {
		if (!uavs[i]) continue;
		bound_add(g_cs_out, &g_cs_out_n, (ID3D11View*)uavs[i], VK_UAV);
		ID3D11UnorderedAccessView_Release(uavs[i]);
	}
	g_cs_dirty = false;
}

void cap_before_call_writes(bool dispatch)
{
	Capture* c = g_bar;
	if (!c || c->tracks.empty())
		return;
	if (dispatch ? g_cs_dirty : g_om_dirty) {
		if (dispatch) refresh_cs_outputs();
		else refresh_om_outputs();
	}
	const BoundOut* list = dispatch ? g_cs_out : g_om_out;
	const uint32_t n = dispatch ? g_cs_out_n : g_om_out_n;
	for (uint32_t i = 0; i < n; i++) {
		auto it = c->tracks.find(list[i].res);
		if (it == c->tracks.end())
			continue;
		ContTrack& t = it->second;
		if (t.buffer)
			cont_buffer_write(c, t, (ID3D11Buffer*)list[i].res, 0, 0xffffffffu);
		else
			cont_texture_write(c, t, (ID3D11Resource*)list[i].res, list[i].mip, list[i].first, list[i].count);
	}
}

void cap_before_unknown_writes()
{
	// Nothing says what a command list writes. Copies that were made stay what
	// they are; whatever is still read in place can no longer be vouched for.
	if (g_bar)
		g_bar->ct_unknown = true;
}

static void cont_release(Capture* c)
{
	for (ContChunk& ch : c->cchunks)
		if (ch.buf) ID3D11Buffer_Release(ch.buf);
	c->cchunks.clear();
	for (ContSnap& s : c->conts) {
		if (s.copy) ID3D11Resource_Release(s.copy);
		s.copy = nullptr;
	}
	if (!c->tracks.empty() || !c->conts.empty())
		c->ct_released = true;
	c->tracks.clear();
	c->out_after.clear();
	if (g_bar == c) {
		g_bar = nullptr;
		InterlockedExchange(&g_barrier, 0);
		g_mapped.clear();
		cont_publish(nullptr);
	}
}

// ---- the list of writes
void cap_event(uint8_t type, ID3D11Resource* dst, ID3D11Resource* src, uint32_t a0, uint32_t a1, uint32_t a2,
               uint32_t a3, const float* f)
{
	Capture* c = g_rec.get();
	if (!c || !c->rq.ct_events)
		return;
	if (c->events.size() >= 100000) {
		c->events_dropped++;
		return;
	}
	CapEvent e;
	memset(&e, 0, sizeof(e));
	e.type = type;
	e.draw = (uint32_t)c->draws.size();          // the call that comes next
	e.dst = dst ? res_id(c, dst) : -1;
	e.src = src ? res_id(c, src) : -1;
	e.a[0] = a0; e.a[1] = a1; e.a[2] = a2; e.a[3] = a3;
	if (f) {
		memcpy(e.f, f, sizeof(e.f));
		e.has_f = true;
	}
	c->events.push_back(e);
}

void cap_event_view(uint8_t type, ID3D11View* view, uint32_t a0, uint32_t a1, const float* f)
{
	if (!view)
		return;
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(view, &r);
	if (!r)
		return;
	cap_event(type, r, nullptr, a0, a1, 0, 0, f);
	ID3D11Resource_Release(r);
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

static void get_samplers(int stage, ID3D11SamplerState** smp)
{
	const UINT N = D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT;
	ID3D11DeviceContext* c = rctx();
	switch (stage) {
	case ST_VS: OCTX(VSGetSamplers)(c, 0, N, smp); break;
	case ST_HS: OCTX(HSGetSamplers)(c, 0, N, smp); break;
	case ST_DS: OCTX(DSGetSamplers)(c, 0, N, smp); break;
	case ST_GS: OCTX(GSGetSamplers)(c, 0, N, smp); break;
	case ST_PS: OCTX(PSGetSamplers)(c, 0, N, smp); break;
	case ST_CS: OCTX(CSGetSamplers)(c, 0, N, smp); break;
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
		if (c->rq.ct_kinds & CK_CB)
			b.cont = cont_buffer(c, cbs[i], b.res);
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
		v.view = view_desc_id(c, (ID3D11View*)srvs[i], VK_SRV, v.res);
		if (c->rq.ct_kinds & (CK_BUF | CK_TEX))
			v.cont = cont_of_view(c, v.res, v.view, (c->rq.ct_kinds & CK_BUF) != 0, (c->rq.ct_kinds & CK_TEX) != 0);
		c->views.push_back(v);
		d->srv_count++;
		ID3D11ShaderResourceView_Release(srvs[i]);
	}

	ID3D11SamplerState* smp[D3D11_COMMONSHADER_SAMPLER_SLOT_COUNT];
	memset(smp, 0, sizeof(smp));
	get_samplers(stage, smp);
	for (UINT i = 0; i < _countof(smp); i++) {
		if (!smp[i]) continue;
		c->samplers.push_back({ (uint8_t)stage, (uint8_t)i, state_id(c, smp[i], SK_SAMPLER) });
		d->smp_count++;
		ID3D11SamplerState_Release(smp[i]);
	}
}

// ================================================================ fixed-function state
static void record_fixed_state(Capture* c, DrawRec* d, ID3D11DeviceContext* ctx)
{
	ID3D11BlendState* bs = nullptr;
	FLOAT factor[4] = {};
	UINT mask = 0;
	OCTX(OMGetBlendState)(ctx, &bs, factor, &mask);
	memcpy(d->blend_factor, factor, sizeof(factor));
	d->sample_mask = mask;
	if (bs) { d->blend = state_id(c, bs, SK_BLEND); ID3D11BlendState_Release(bs); }

	ID3D11DepthStencilState* dss = nullptr;
	UINT ref = 0;
	OCTX(OMGetDepthStencilState)(ctx, &dss, &ref);
	d->stencil_ref = ref;
	if (dss) { d->depth = state_id(c, dss, SK_DEPTH); ID3D11DepthStencilState_Release(dss); }

	ID3D11RasterizerState* rs = nullptr;
	OCTX(RSGetState)(ctx, &rs);
	if (rs) { d->raster = state_id(c, rs, SK_RASTER); ID3D11RasterizerState_Release(rs); }

	D3D11_RECT rects[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
	UINT nrect = _countof(rects);
	memset(rects, 0, sizeof(rects));
	OCTX(RSGetScissorRects)(ctx, &nrect, rects);
	if (nrect > _countof(rects)) nrect = _countof(rects);
	d->sc_begin = (uint32_t)c->rects.size();
	d->sc_count = nrect;
	for (UINT i = 0; i < nrect; i++)
		c->rects.push_back(rects[i]);

	D3D11_VIEWPORT vps[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
	UINT nvp = _countof(vps);
	memset(vps, 0, sizeof(vps));
	OCTX(RSGetViewports)(ctx, &nvp, vps);
	if (nvp > _countof(vps)) nvp = _countof(vps);
	d->vp_count = nvp;
	if (nvp > 1) {
		d->vp_begin = (uint32_t)c->viewports.size();
		for (UINT i = 0; i < nvp; i++)
			c->viewports.push_back(vps[i]);
	}
	if (nvp) {
		d->vp[0] = vps[0].TopLeftX; d->vp[1] = vps[0].TopLeftY; d->vp[2] = vps[0].Width; d->vp[3] = vps[0].Height;
		d->vp_z[0] = vps[0].MinDepth; d->vp_z[1] = vps[0].MaxDepth;
		d->has_vp = true;
	}
	d->has_state = true;
}

// ================================================================ render target copies
static bool draw_matches(Capture* c, const DrawRec& d, const std::vector<DrawMatch>& list)
{
	if (list.empty())
		return false;
	uint64_t vs = 0, ps = 0, cs = 0;
	if (d.shaders[ST_VS] >= 0 && c->shaders[d.shaders[ST_VS]].known) vs = c->shaders[d.shaders[ST_VS]].info.hash;
	if (d.shaders[ST_PS] >= 0 && c->shaders[d.shaders[ST_PS]].known) ps = c->shaders[d.shaders[ST_PS]].info.hash;
	if (d.shaders[ST_CS] >= 0 && c->shaders[d.shaders[ST_CS]].known) cs = c->shaders[d.shaders[ST_CS]].info.hash;
	const CapRes* ib = d.ib_res >= 0 ? &c->res[d.ib_res] : nullptr;
	for (const DrawMatch& m : list) {
		if (m.draw >= 0 && (uint32_t)m.draw != d.index) continue;
		if (m.first >= 0 && d.index < (uint32_t)m.first) continue;
		if (m.last >= 0 && d.index > (uint32_t)m.last) continue;
		if (m.ib && (!ib || ib->ptr != m.ib)) continue;
		if (m.ib_serial && (!ib || ib->serial != m.ib_serial)) continue;
		if (m.index_count && d.a.index_count != m.index_count) continue;
		if (m.first_index >= 0 && d.a.first_index != (uint32_t)m.first_index) continue;
		if (m.vs && vs != m.vs) continue;
		if (m.ps && ps != m.ps) continue;
		if (m.shader && vs != m.shader && ps != m.shader && cs != m.shader) continue;
		if (m.origin >= 0 && d.caller.origin != m.origin) continue;
		return true;
	}
	return false;
}

// Queues a copy of what the view shows into a staging texture of its own. Like
// the buffer copies it is only read back once a client asks for it.
static void rt_copy(Capture* c, uint32_t draw, uint8_t when, uint8_t kind, uint8_t slot, ID3D11View* view)
{
	RtSnap s;
	memset(&s, 0, sizeof(s));
	s.draw = draw; s.when = when; s.kind = kind; s.slot = slot;
	s.state = RT_FAILED;
	s.res = -1;
	ID3D11Resource* r = nullptr;
	ID3D11View_GetResource(view, &r);
	if (!r) { c->rts.push_back(s); return; }
	s.res = res_id(c, r);
	D3D11_RESOURCE_DIMENSION dim = D3D11_RESOURCE_DIMENSION_UNKNOWN;
	ID3D11Resource_GetType(r, &dim);
	if (dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D) {
		s.state = RT_NOT_2D;
		ID3D11Resource_Release(r);
		c->rts.push_back(s);
		return;
	}
	ID3D11Texture2D_GetDesc((ID3D11Texture2D*)r, &s.desc);
	s.format = s.desc.Format;
	bool whole = false;          // depth-stencil data can only be copied as a whole subresource
	if (kind == OUT_RTV) {
		D3D11_RENDER_TARGET_VIEW_DESC vd;
		ID3D11RenderTargetView_GetDesc((ID3D11RenderTargetView*)view, &vd);
		if (vd.Format != DXGI_FORMAT_UNKNOWN) s.format = vd.Format;
		if (vd.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2D) s.mip = vd.Texture2D.MipSlice;
		else if (vd.ViewDimension == D3D11_RTV_DIMENSION_TEXTURE2DARRAY) { s.mip = vd.Texture2DArray.MipSlice; s.slice = vd.Texture2DArray.FirstArraySlice; }
	} else {
		D3D11_DEPTH_STENCIL_VIEW_DESC vd;
		ID3D11DepthStencilView_GetDesc((ID3D11DepthStencilView*)view, &vd);
		if (vd.Format != DXGI_FORMAT_UNKNOWN) s.format = vd.Format;
		if (vd.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2D) s.mip = vd.Texture2D.MipSlice;
		else if (vd.ViewDimension == D3D11_DSV_DIMENSION_TEXTURE2DARRAY) { s.mip = vd.Texture2DArray.MipSlice; s.slice = vd.Texture2DArray.FirstArraySlice; }
		whole = true;
	}
	ID3D11Texture2D* st = nullptr;
	if (s.desc.SampleDesc.Count > 1) {
		s.state = RT_MULTISAMPLED;
	} else if (s.mip < s.desc.MipLevels && s.slice < s.desc.ArraySize) {
		UINT mw = std::max(s.desc.Width >> s.mip, (UINT)1), mh = std::max(s.desc.Height >> s.mip, (UINT)1);
		D3D11_BOX box = { 0, 0, 0, mw, mh, 1 };
		if (c->rq.rt_has_box && !whole) {
			D3D11_BOX b = { std::min(c->rq.rt_box[0], mw), std::min(c->rq.rt_box[1], mh), 0,
			                std::min(c->rq.rt_box[2], mw), std::min(c->rq.rt_box[3], mh), 1 };
			if (b.right > b.left && b.bottom > b.top) { box = b; s.boxed = true; }
		}
		s.w = box.right - box.left; s.h = box.bottom - box.top;
		s.box[0] = box.left; s.box[1] = box.top; s.box[2] = box.right; s.box[3] = box.bottom;
		size_t num_bytes = 0, row_bytes = 0, rows = 0;
		dxgi_surface_info(s.w, s.h, s.desc.Format, &num_bytes, &row_bytes, &rows);
		s.bytes = num_bytes;
		// A copy from before the draw is only worth its memory together with the one from after it.
		const uint64_t needed = when == 0 ? 2 * (uint64_t)num_bytes : num_bytes;
		if (!num_bytes) {
			s.state = RT_FAILED;
		} else if (c->rt_bytes + needed > (uint64_t)c->rq.rt_budget_mb * 1024 * 1024) {
			s.state = RT_BUDGET;
			c->rt_skipped++;
		} else {
			D3D11_TEXTURE2D_DESC sd;
			memset(&sd, 0, sizeof(sd));
			sd.Width = s.w; sd.Height = s.h;
			sd.MipLevels = 1; sd.ArraySize = 1;
			sd.Format = s.desc.Format;
			sd.SampleDesc.Count = 1;
			sd.Usage = D3D11_USAGE_STAGING;
			sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
			if (SUCCEEDED(ODEV(CreateTexture2D)(rdev(), &sd, nullptr, &st)) && st) {
				OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)st, 0, 0, 0, 0, r,
				                            s.mip + s.slice * s.desc.MipLevels, s.boxed ? &box : nullptr);
				s.staging = st;
				s.state = RT_OK;
				c->rt_bytes += num_bytes;
			}
		}
	}
	ID3D11Resource_Release(r);
	c->rts.push_back(s);
}

static void rt_select(Capture* c, uint32_t draw, uint8_t kind, uint8_t slot, ID3D11View* view)
{
	if (c->rq.rt_before)
		rt_copy(c, draw, 0, kind, slot, view);
	ID3D11View_AddRef(view);
	c->rt_after.push_back({ kind, slot, view });
}

static void rt_drop_pending(Capture* c)
{
	for (RtAfter& t : c->rt_after)
		ID3D11View_Release(t.view);
	c->rt_after.clear();
}

static bool rt_holds_copies(const Capture* c)
{
	for (const RtSnap& s : c->rts)
		if (s.staging) return true;
	return false;
}

static void rt_release_copies(Capture* c)
{
	for (RtSnap& s : c->rts) {
		if (s.staging) ID3D11Texture2D_Release(s.staging);
		s.staging = nullptr;
	}
}

// Which of a call's outputs are to be noted once it has run (CK_* bits).
static uint32_t want_outputs(Capture* c, const DrawRec& d)
{
	c->out_after.clear();            // nothing is left unless a call never returned to its hook
	if (!c->rq.ct_out_kinds)
		return 0;
	return c->rq.ct_out_all || draw_matches(c, d, c->rq.ct_match) ? c->rq.ct_out_kinds : 0;
}

void cap_after_draw()
{
	InterlockedExchange(&g_after, 0);
	Capture* c = g_rec.get();
	if (!c)
		return;
	for (RtAfter& t : c->rt_after)
		rt_copy(c, c->rt_after_draw, 1, t.kind, t.slot, t.view);
	rt_drop_pending(c);
	// What the call left behind: the version that stands until the next write.
	for (const OutAfter& o : c->out_after) {
		ViewBind& v = c->views[o.bind];
		v.cont = cont_of_view(c, v.res, v.view, true, true);
	}
	c->out_after.clear();
}

// ================================================================ slot cache
// Every so often a frame is looked at for the slot cache (slotcache.cpp). In that frame only
// indexed draws that were not seen before are recorded: the index buffer region, the shaders,
// what is bound to the vertex and pixel shader resource slots and the output formats. A draw
// that was seen costs the queries that tell it apart and one lookup.
static std::unordered_set<SlotSeen, SlotSeenHash> g_slot_seen;   // render thread
static LONG g_slot_seen_gen = -1;
static std::unordered_map<void*, bool> g_layout_skinned;          // input layout -> has BLENDINDICES
static uint32_t g_slot_frames = 0;

static bool layout_skinned(void* il)
{
	auto it = g_layout_skinned.find(il);
	if (it != g_layout_skinned.end())
		return it->second;
	LayoutInfo li;
	bool skinned = false;
	if (reg_get_layout(il, &li))
		for (const LayoutElem& e : li.elems)
			if (_stricmp(e.semantic, "BLENDINDICES") == 0) { skinned = true; break; }
	g_layout_skinned[il] = skinned;
	return skinned;
}

static void slot_prepare()
{
	LONG gen = slot_generation();
	if (gen != g_slot_seen_gen || g_slot_seen.size() > 200000) {
		g_slot_seen.clear();
		g_slot_seen_gen = gen;
	}
	// An address can be taken over by another input layout: what is known about them is
	// forgotten now and then, which costs a registry lookup per layout.
	if (++g_slot_frames % 64 == 0)
		g_layout_skinned.clear();
}

static void slot_srvs(Capture* c, DrawRec* d, int stage)
{
	static ID3D11ShaderResourceView* srvs[D3D11_COMMONSHADER_INPUT_RESOURCE_SLOT_COUNT];
	memset(srvs, 0, sizeof(srvs));
	get_srvs(stage, srvs);
	for (UINT i = 0; i < _countof(srvs); i++) {
		if (!srvs[i]) continue;
		ViewBind v;
		v.stage = (uint8_t)stage;
		v.slot = (uint8_t)i;
		v.res = view_res_id(c, (ID3D11View*)srvs[i]);
		v.view = view_desc_id(c, (ID3D11View*)srvs[i], VK_SRV, v.res);
		c->views.push_back(v);
		d->srv_count++;
		ID3D11ShaderResourceView_Release(srvs[i]);
	}
}

// LoyalTools takes a draw for a character's only when a buffer (the bone pool) is bound at vs-t0.
static bool vs_t0_is_buffer(ID3D11DeviceContext* ctx)
{
	ID3D11ShaderResourceView* srv = nullptr;
	OCTX(VSGetShaderResources)(ctx, 0, 1, &srv);
	if (!srv)
		return false;
	ID3D11Resource* res = nullptr;
	ID3D11View_GetResource((ID3D11View*)srv, &res);
	bool buffer = false;
	if (res) {
		D3D11_RESOURCE_DIMENSION dim = D3D11_RESOURCE_DIMENSION_UNKNOWN;
		ID3D11Resource_GetType(res, &dim);
		buffer = dim == D3D11_RESOURCE_DIMENSION_BUFFER;
		ID3D11Resource_Release(res);
	}
	ID3D11ShaderResourceView_Release(srv);
	return buffer;
}

static const char* usage_name(uint32_t usage)
{
	switch (usage) {
	case D3D11_USAGE_DEFAULT: return "default";
	case D3D11_USAGE_IMMUTABLE: return "immutable";
	case D3D11_USAGE_DYNAMIC: return "dynamic";
	case D3D11_USAGE_STAGING: return "staging";
	default: return "unknown";
	}
}

static void slot_record(Capture* c, uint8_t type, const DrawArgs& a)
{
	if (type == DT_DISPATCH || type == DT_DISPATCH_INDIRECT || !a.index_count)
		return;
	ID3D11DeviceContext* ctx = rctx();
	ID3D11InputLayout* il = nullptr;
	OCTX(IAGetInputLayout)(ctx, &il);
	const bool skinned = il && layout_skinned(il);
	if (slot_skinned_only() && !skinned) {
		if (il) ID3D11InputLayout_Release(il);
		return;
	}
	ID3D11Buffer* ib = nullptr;
	DXGI_FORMAT fmt = DXGI_FORMAT_UNKNOWN;
	UINT ib_off = 0;
	OCTX(IAGetIndexBuffer)(ctx, &ib, &fmt, &ib_off);
	if (!ib) {
		if (il) ID3D11InputLayout_Release(il);
		return;
	}
	const int32_t rid = res_id(c, (ID3D11Resource*)ib);
	if (slot_static_only() && c->res[rid].live.buf.Usage == D3D11_USAGE_DYNAMIC) {
		// Index data the CPU rewrites (the game's dynamic batches): another hash every frame, never
		// a mesh a mod could name.
		ID3D11Buffer_Release(ib);
		if (il) ID3D11InputLayout_Release(il);
		return;
	}
	ID3D11VertexShader* vs = nullptr;
	OCTX(VSGetShader)(ctx, &vs, nullptr, nullptr);
	ID3D11PixelShader* ps = nullptr;
	OCTX(PSGetShader)(ctx, &ps, nullptr, nullptr);

	const int32_t vsid = shader_id(c, vs), psid = shader_id(c, ps);
	SlotSeen k;
	k.ib = ib;
	k.ib_sn = c->res[rid].serial;
	k.ib_off = ib_off;
	k.ib_fmt = (uint32_t)fmt;
	k.first = a.first_index;
	k.count = a.index_count;
	k.vs_sn = vsid >= 0 && c->shaders[vsid].known ? c->shaders[vsid].info.serial : (uint64_t)(uintptr_t)vs;
	k.ps_sn = psid >= 0 && c->shaders[psid].known ? c->shaders[psid].info.serial : (uint64_t)(uintptr_t)ps;

	if (g_slot_seen.find(k) == g_slot_seen.end()) {
		if (slot_bones_only() && !vs_t0_is_buffer(ctx)) {
			g_slot_seen.insert(k);         // not a character's draw: not asked about again
		} else if (c->slot_new >= slot_max_new()) {
			c->slot_deferred++;
		} else {
			g_slot_seen.insert(k);
			c->slot_new++;
			DrawRec d;
			d.index = (uint32_t)c->draws.size();
			d.type = type;
			d.a = a;
			caller_from_probe(&d.caller);      // a stack walk per new draw rather than per draw
			d.ib_res = rid;
			d.ib_fmt = (uint32_t)fmt;
			d.ib_off = ib_off;
			uint32_t ro, rs;
			ib_region(fmt, ib_off, a, &ro, &rs);
			d.ib_region = region(c, ib, rid, ro, rs);
			if (il) d.layout = layout_id(c, il);
			d.shaders[ST_VS] = vsid;
			d.shaders[ST_PS] = psid;
			d.srv_begin = (uint32_t)c->views.size();
			slot_srvs(c, &d, ST_VS);
			slot_srvs(c, &d, ST_PS);
			d.out_begin = (uint32_t)c->views.size();
			ID3D11RenderTargetView* rtvs[D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT];
			ID3D11DepthStencilView* dsv = nullptr;
			memset(rtvs, 0, sizeof(rtvs));
			OCTX(OMGetRenderTargets)(ctx, D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtvs, &dsv);
			for (UINT i = 0; i < _countof(rtvs); i++) {
				if (!rtvs[i]) continue;
				int32_t orid = view_res_id(c, (ID3D11View*)rtvs[i]);
				c->views.push_back({ (uint8_t)OUT_RTV, (uint8_t)i, orid, view_desc_id(c, (ID3D11View*)rtvs[i], VK_RTV, orid) });
				d.out_count++;
				ID3D11RenderTargetView_Release(rtvs[i]);
			}
			if (dsv) {
				int32_t orid = view_res_id(c, (ID3D11View*)dsv);
				c->views.push_back({ (uint8_t)OUT_DSV, 0, orid, view_desc_id(c, (ID3D11View*)dsv, VK_DSV, orid) });
				d.out_count++;
				ID3D11DepthStencilView_Release(dsv);
			}
			c->draws.push_back(d);
			c->slot_keys.push_back(k);
		}
	}
	if (ps) ID3D11PixelShader_Release(ps);
	if (vs) ID3D11VertexShader_Release(vs);
	ID3D11Buffer_Release(ib);
	if (il) ID3D11InputLayout_Release(il);
}

static const char* region_state_name(uint8_t s)
{
	switch (s) {
	case RS_OK: return "ok";
	case RS_CACHED: return "cached";
	case RS_OOB: return "out_of_bounds";
	case RS_SKIPPED: return "skipped";
	case RS_VOLATILE: return "volatile";
	case RS_PENDING: return "pending";
	default: return "none";
	}
}

static const char* dim_name(uint8_t dim)
{
	switch (dim) {
	case D3D11_RESOURCE_DIMENSION_BUFFER: return "buffer";
	case D3D11_RESOURCE_DIMENSION_TEXTURE1D: return "tex1d";
	case D3D11_RESOURCE_DIMENSION_TEXTURE2D: return "tex2d";
	case D3D11_RESOURCE_DIMENSION_TEXTURE3D: return "tex3d";
	default: return "unknown";
	}
}

// One bound resource as the slot cache writes it; `sig` gathers what tells bindings apart.
static void slot_view_json(JsonW& w, const Capture& c, const ViewBind& v, std::string* sig)
{
	char b[160];
	w.begin_obj();
	w.ku("slot", v.slot);
	if (v.res >= 0) {
		const CapRes& r = c.res[v.res];
		w.ks("kind", dim_name(r.dim));
		if (r.known) w.kh32("hash", r.hash);
		w.ks("origin", origin_name(r.origin));
		uint32_t fmt = 0, sw = 0, sh = 0;
		switch (r.dim) {
		case D3D11_RESOURCE_DIMENSION_BUFFER:
			w.ku("size", r.live.buf.ByteWidth);
			if (r.live.buf.StructureByteStride) w.ku("stride", r.live.buf.StructureByteStride);
			sw = r.live.buf.ByteWidth;
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE1D:
			fmt = r.live.t1.Format; sw = r.live.t1.Width;
			w.ku("w", sw); w.ku("mips", r.live.t1.MipLevels); w.ku("array", r.live.t1.ArraySize);
			w.ks("format", dxgi_format_name((DXGI_FORMAT)fmt));
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE2D:
			fmt = r.live.t2.Format; sw = r.live.t2.Width; sh = r.live.t2.Height;
			w.ku("w", sw); w.ku("h", sh); w.ku("mips", r.live.t2.MipLevels); w.ku("array", r.live.t2.ArraySize);
			if (r.live.t2.SampleDesc.Count > 1) w.ku("samples", r.live.t2.SampleDesc.Count);
			w.ks("format", dxgi_format_name((DXGI_FORMAT)fmt));
			break;
		case D3D11_RESOURCE_DIMENSION_TEXTURE3D:
			fmt = r.live.t3.Format; sw = r.live.t3.Width; sh = r.live.t3.Height;
			w.ku("w", sw); w.ku("h", sh); w.ku("d", r.live.t3.Depth); w.ku("mips", r.live.t3.MipLevels);
			w.ks("format", dxgi_format_name((DXGI_FORMAT)fmt));
			break;
		default: break;
		}
		snprintf(b, sizeof(b), "%u:%u:%u:%u:%u;", v.stage, v.slot, (unsigned)r.dim, fmt,
		         r.dim == D3D11_RESOURCE_DIMENSION_BUFFER ? r.live.buf.StructureByteStride : 0u);
		*sig += b;
	}
	if (v.view >= 0) {
		const CapView& cv = c.view_descs[v.view];
		w.ks("view_format", dxgi_format_name((DXGI_FORMAT)cv.format));
		w.ku("view_dim", cv.dim);
	}
	w.end_obj();
}

// A looked-at frame is finished: one record per draw that was new in it.
static void slot_push(const Capture& c)
{
	FILETIME ft_now;
	GetSystemTimeAsFileTime(&ft_now);
	const uint64_t ft = ((uint64_t)ft_now.dwHighDateTime << 32) | ft_now.dwLowDateTime;
	const uint64_t unix_ms = (ft - 116444736000000000ull) / 10000;
	for (size_t i = 0; i < c.draws.size(); i++) {
		const DrawRec& d = c.draws[i];
		uint8_t st = 0xff;
		uint32_t ib_hash = 0;
		if (d.ib_region >= 0) {
			st = c.regions[d.ib_region].state;
			ib_hash = c.regions[d.ib_region].hash;
		}
		const bool hashed = st == RS_OK || st == RS_CACHED;
		if (st == RS_SKIPPED || st == RS_PENDING) {
			// Not read back this time (the staging memory was full): looked at again later.
			if (i < c.slot_keys.size())
				g_slot_seen.erase(c.slot_keys[i]);
			continue;
		}
		const int32_t vsid = d.shaders[ST_VS], psid = d.shaders[ST_PS];
		const uint64_t vs = vsid >= 0 && c.shaders[vsid].known ? c.shaders[vsid].info.hash : 0;
		const uint64_t ps = psid >= 0 && c.shaders[psid].known ? c.shaders[psid].info.hash : 0;

		JsonW w;
		w.begin_obj();
		w.ku("fmt", 2);
		w.ku("frame", c.frame);
		w.ku("unix_ms", unix_ms);
		w.ks("origin", origin_name(d.caller.origin));
		w.ku("draw_type", d.type);
		w.key("ib"); if (hashed) w.hex32(ib_hash); else w.null();
		w.ks("ib_state", region_state_name(st));
		if (d.ib_res >= 0 && c.res[d.ib_res].known) w.kh32("ib_resource", c.res[d.ib_res].hash);
		w.ks("ib_format", dxgi_format_name((DXGI_FORMAT)d.ib_fmt));
		if (d.ib_res >= 0) w.ks("ib_usage", usage_name(c.res[d.ib_res].live.buf.Usage));
		w.ku("ib_offset", d.ib_off);
		w.ku("first_index", d.a.first_index);
		w.ku("index_count", d.a.index_count);
		w.ki("base_vertex", (int32_t)d.a.first_vertex);
		w.ku("instances", d.a.instance_count ? d.a.instance_count : 1);
		w.key("vs"); if (vs) w.hex64(vs); else w.null();
		w.key("ps"); if (ps) w.hex64(ps); else w.null();
		bool skinned = false;
		if (d.layout >= 0 && c.layouts[d.layout].known) {
			w.key("layout");
			w.begin_arr();
			for (const LayoutElem& e : c.layouts[d.layout].info.elems) {
				char sem[64];
				snprintf(sem, sizeof(sem), "%s%u", e.semantic, e.index);
				w.str(sem);
				if (_stricmp(e.semantic, "BLENDINDICES") == 0) skinned = true;
			}
			w.end_arr();
		}
		w.kb("skinned", skinned);
		std::string sig;
		for (int stage : { (int)ST_VS, (int)ST_PS }) {
			w.key(stage == ST_VS ? "vs_srv" : "ps_srv");
			w.begin_arr();
			for (uint32_t j = d.srv_begin; j < d.srv_begin + d.srv_count; j++)
				if (c.views[j].stage == stage)
					slot_view_json(w, c, c.views[j], &sig);
			w.end_arr();
		}
		w.key("rt");
		w.begin_arr();
		const char* ds = nullptr;
		for (uint32_t j = d.out_begin; j < d.out_begin + d.out_count; j++) {
			const ViewBind& v = c.views[j];
			const char* f = v.view >= 0 ? dxgi_format_name((DXGI_FORMAT)c.view_descs[v.view].format) : "unknown";
			if (v.stage == OUT_RTV) w.str(f);
			else if (v.stage == OUT_DSV) ds = f;
			sig += v.stage == OUT_RTV ? "o:" : "d:";
			sig += f;
			sig += ';';
		}
		w.end_arr();
		w.key("ds"); if (ds) w.str(ds); else w.null();

		char key[160];
		snprintf(key, sizeof(key), "%s-%08x-%u-%016llx-%016llx-%016llx", origin_name(d.caller.origin),
		         hashed ? ib_hash : 0u, d.a.index_count, (unsigned long long)vs, (unsigned long long)ps,
		         (unsigned long long)fnv64(sig.data(), sig.size()));
		w.ks("key", key);
		w.end_obj();
		slot_submit(std::move(w.s), std::string(key), vs, ps);
	}
	slot_note_frame(c.record_ms + c.finalize_ms, c.slot_new, c.slot_deferred);
}

// ================================================================ trigger, as draws are issued
static uint64_t pair_key(uint64_t vs, uint64_t ps)
{
	return vs * 0x9E3779B97F4A7C15ull ^ (ps + 0x632BE59BD9B4E019ull + (vs << 6) + (vs >> 2));
}

static bool trig_counts(const DrawRec& d)
{
	if (t_spec.origin >= 0 && d.caller.origin != t_spec.origin)
		return false;
	return !t_spec.min_count || (d.a.index_count ? d.a.index_count : d.a.vertex_count) >= t_spec.min_count;
}

// Draw d of capture c showed what the trigger waits for. `late`: only the index data read back
// after the frame showed it, so the frame to record is the one starting now.
static void trig_fire(const Capture* c, const DrawRec& d, const char* what, uint32_t ib, uint64_t shader, bool late)
{
	t_phase = TRIG_FIRED;
	t_fire_frame = late ? (uint32_t)g_frame_no - 1 : (uint32_t)g_frame_no;
	InterlockedExchange(&g_trig_watch, 0);
	TrigFire f;
	f.frame = c->frame;
	f.draw = d.index;
	f.type = d.type;
	f.origin = d.caller.origin;
	f.count = d.a.index_count ? d.a.index_count : d.a.vertex_count;
	f.late = late;
	f.what = what;
	f.ib = ib;
	f.shader = shader;
	if (d.shaders[ST_VS] >= 0 && c->shaders[d.shaders[ST_VS]].known) f.vs = c->shaders[d.shaders[ST_VS]].info.hash;
	if (d.shaders[ST_PS] >= 0 && c->shaders[d.shaders[ST_PS]].known) f.ps = c->shaders[d.shaders[ST_PS]].info.hash;
	EnterCriticalSection(&g_lock);
	if (g_trig_state == TRIG_ARMED && g_trig_id == t_id) {
		g_trig_state = TRIG_FIRED;
		g_trig_fire = f;
		g_trig_fired_ms = now_ms();
	}
	LeaveCriticalSection(&g_lock);
}

// Called for every draw recorded while the trigger watches. Shader hashes are known as the draw is
// issued; an index buffer hash only when it was remembered from an earlier frame (otherwise see
// trig_check_late).
static void trig_check(const Capture* c, const DrawRec& d)
{
	if ((t_phase != TRIG_BASELINE && t_phase != TRIG_ARMED) || !trig_counts(d))
		return;
	uint64_t sh[ST_COUNT] = {};
	for (int s = 0; s < ST_COUNT; s++)
		if (d.shaders[s] >= 0 && c->shaders[d.shaders[s]].known)
			sh[s] = c->shaders[d.shaders[s]].info.hash;
	if (t_phase == TRIG_BASELINE) {
		if (t_spec.new_pairs && sh[ST_VS] && sh[ST_PS])
			t_pairs.insert(pair_key(sh[ST_VS], sh[ST_PS]));
		return;
	}
	for (int s = 0; s < ST_COUNT; s++)
		if (sh[s] && t_shader.count(sh[s]))
			return trig_fire(c, d, "shader", 0, sh[s], false);
	if (!t_ib.empty() && d.ib_region >= 0) {
		const CapRegion& r = c->regions[d.ib_region];
		if ((r.state == RS_CACHED || r.state == RS_OK) && t_ib.count(r.hash))
			return trig_fire(c, d, "ib", r.hash, 0, false);
	}
	if (t_spec.new_pairs && sh[ST_VS] && sh[ST_PS] && !t_pairs.count(pair_key(sh[ST_VS], sh[ST_PS])))
		trig_fire(c, d, "new pair", 0, 0, false);
}

// A watched frame whose index data has been read back: hashes seen for the first time.
static void trig_check_late(const Capture* c)
{
	if (t_phase != TRIG_ARMED || t_ib.empty() || c->trig_id != t_id)
		return;
	for (const DrawRec& d : c->draws) {
		if (d.ib_region < 0 || !trig_counts(d))
			continue;
		const CapRegion& r = c->regions[d.ib_region];
		if (r.state == RS_OK && t_ib.count(r.hash))
			return trig_fire(c, d, "ib", r.hash, 0, true);
	}
}

// A draw of a recorded frame: a vertex/pixel shader pair of the game's that the recording has not seen
// yet (after its first frames) is news, and asks for a keyframe.
static void rec_check(const Capture* c, const DrawRec& d)
{
	if (d.caller.origin == ORIGIN_XXMI || d.shaders[ST_VS] < 0 || d.shaders[ST_PS] < 0)
		return;
	const auto& vs = c->shaders[d.shaders[ST_VS]];
	const auto& ps = c->shaders[d.shaders[ST_PS]];
	if (!vs.known || !ps.known || !r_pairs.insert(pair_key(vs.info.hash, ps.info.hash)).second)
		return;
	if (c->frame - r_start_frame < k_rec_baseline)
		return;
	r_news++;
	if (!r_news_frame) {
		r_news_frame = c->frame;
		char b[96];
		snprintf(b, sizeof(b), "draw %u: vs %016llx ps %016llx", d.index, (unsigned long long)vs.info.hash,
		         (unsigned long long)ps.info.hash);
		r_news_what = b;
	}
}

void cap_on_draw(uint8_t type, const DrawArgs& a, const uint32_t* thread_groups,
                 void* indirect_buffer, uint32_t indirect_offset, const CallerInfo& caller)
{
	Capture* c = g_rec.get();
	if (!c || !g_ctx || !g_dev)
		return;
	if (c->slots) {
		double t0 = qpc_ms();
		slot_record(c, type, a);
		c->record_ms += qpc_ms() - t0;
		return;
	}
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
		// The arguments the GPU will read: five, four or three 32-bit values.
		if (full)
			d.indirect_snap = snapshot(c, (ID3D11Buffer*)indirect_buffer, d.indirect_res, indirect_offset,
			                           type == DT_DRAW_INDEXED_INSTANCED_INDIRECT ? 20 : type == DT_DRAW_INSTANCED_INDIRECT ? 16 : 12);
	}
	d.vb_begin = (uint32_t)c->vbs.size();
	d.cb_begin = (uint32_t)c->cbs.size();
	d.srv_begin = (uint32_t)c->views.size();
	d.smp_begin = (uint32_t)c->samplers.size();

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
			if (c->rq.ct_kinds & CK_IB)
				d.ib_cont = cont_buffer(c, ib, d.ib_res);
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
			if (c->rq.ct_kinds & CK_VB)
				b.cont = cont_buffer(c, vbs[i], b.res);
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
			static ID3D11UnorderedAccessView* om_uavs[D3D11_1_UAV_SLOT_COUNT];
			memset(rtvs, 0, sizeof(rtvs));
			memset(om_uavs, 0, sizeof(om_uavs));
			const UINT n_uav = uav_slots();
			OCTX(OMGetRenderTargetsAndUnorderedAccessViews)(ctx, D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT, rtvs, &dsv,
			                                                0, n_uav, om_uavs);
			// Render target copies: for the first rt_max selected draws that have a target to copy.
			const bool matched = draw_matches(c, d, c->rq.rt_match);
			const bool copy_targets = matched && c->rt_draws < c->rq.rt_max;
			if (matched) c->rt_matched++;
			rt_drop_pending(c);          // nothing is pending unless a draw never returned to its hook
			const uint32_t keep = want_outputs(c, d);
			bool colour = false;
			for (UINT i = 0; i < _countof(rtvs); i++) {
				if (!rtvs[i]) continue;
				int32_t rid = view_res_id(c, (ID3D11View*)rtvs[i]);
				if (keep & CK_RT) c->out_after.push_back({ (uint32_t)c->views.size() });
				c->views.push_back({ (uint8_t)OUT_RTV, (uint8_t)i, rid, view_desc_id(c, (ID3D11View*)rtvs[i], VK_RTV, rid) });
				d.out_count++;
				if (copy_targets && (!colour || c->rq.rt_all))
					rt_select(c, d.index, OUT_RTV, (uint8_t)i, (ID3D11View*)rtvs[i]);
				colour = true;
				ID3D11RenderTargetView_Release(rtvs[i]);
			}
			if (dsv) {
				int32_t rid = view_res_id(c, (ID3D11View*)dsv);
				if (keep & CK_DEPTH) c->out_after.push_back({ (uint32_t)c->views.size() });
				c->views.push_back({ (uint8_t)OUT_DSV, 0, rid, view_desc_id(c, (ID3D11View*)dsv, VK_DSV, rid) });
				d.out_count++;
				if (copy_targets && c->rq.rt_depth)
					rt_select(c, d.index, OUT_DSV, 0, (ID3D11View*)dsv);
				ID3D11DepthStencilView_Release(dsv);
			}
			// What a pixel shader writes besides its render targets.
			for (UINT i = 0; i < n_uav; i++) {
				if (!om_uavs[i]) continue;
				int32_t rid = view_res_id(c, (ID3D11View*)om_uavs[i]);
				if (keep & CK_UAV) c->out_after.push_back({ (uint32_t)c->views.size() });
				c->views.push_back({ (uint8_t)OUT_OM_UAV, (uint8_t)i, rid, view_desc_id(c, (ID3D11View*)om_uavs[i], VK_UAV, rid) });
				d.out_count++;
				ID3D11UnorderedAccessView_Release(om_uavs[i]);
			}
			if (!c->rt_after.empty()) {
				// The hook that called us copies them again once the draw has been issued.
				c->rt_draws++;
				c->rt_after_draw = d.index;
				InterlockedExchange(&g_after, 1);
			} else if (matched && !colour && !c->rq.rt_depth) {
				c->rt_depth_only++;      // a shadow or depth pass: it does not count towards the limit
			}
			if (!c->out_after.empty()) {
				c->ct_out_calls++;
				InterlockedExchange(&g_after, 1);
			}

			record_fixed_state(c, &d, ctx);
		}
	} else {
		ID3D11ComputeShader* cs = nullptr;
		OCTX(CSGetShader)(ctx, &cs, nullptr, nullptr);
		if (cs) { d.shaders[ST_CS] = shader_id(c, cs); ID3D11ComputeShader_Release(cs); record_stage(c, &d, ST_CS, full); }
		if (full) {
			d.out_begin = (uint32_t)c->views.size();
			ID3D11UnorderedAccessView* uavs[D3D11_1_UAV_SLOT_COUNT];
			memset(uavs, 0, sizeof(uavs));
			UINT n = uav_slots();
			OCTX(CSGetUnorderedAccessViews)(ctx, 0, n, uavs);
			rt_drop_pending(c);
			const uint32_t keep = want_outputs(c, d);
			for (UINT i = 0; i < n; i++) {
				if (!uavs[i]) continue;
				int32_t rid = view_res_id(c, (ID3D11View*)uavs[i]);
				if (keep & CK_UAV) c->out_after.push_back({ (uint32_t)c->views.size() });
				c->views.push_back({ (uint8_t)OUT_CS_UAV, (uint8_t)i, rid, view_desc_id(c, (ID3D11View*)uavs[i], VK_UAV, rid) });
				d.out_count++;
				ID3D11UnorderedAccessView_Release(uavs[i]);
			}
			if (!c->out_after.empty()) {
				c->ct_out_calls++;
				InterlockedExchange(&g_after, 1);
			}
		}
	}

	if (!c->trig) {
		if (g_trig_watch)
			trig_check(c, d);
		if (c->rec)
			rec_check(c, d);
	}
	c->draws.push_back(d);
	c->record_ms += qpc_ms() - t0;
}

// ================================================================ continuous mode
static uint32_t live_key_id_locked(const LiveKey& k)
{
	auto it = g_live_key_ids.find(k);
	if (it != g_live_key_ids.end())
		return it->second;
	uint32_t id = (uint32_t)g_live_keys.size();
	g_live_keys.push_back(k);
	g_live_key_ids.emplace(k, id);
	return id;
}

// Once there are many keys, the ones no kept frame uses are dropped and the rest renumbered.
static void live_compact_keys_locked()
{
	if (g_live_keys.size() < g_live_compact_at)
		return;
	std::vector<uint32_t> remap(g_live_keys.size(), UINT32_MAX);
	std::vector<LiveKey> keys;
	for (LiveFrame& f : g_live_ring) {
		for (LiveEntry& e : f.items) {
			if (remap[e.key] == UINT32_MAX) {
				remap[e.key] = (uint32_t)keys.size();
				keys.push_back(g_live_keys[e.key]);
			}
			e.key = remap[e.key];
		}
	}
	g_live_keys.swap(keys);
	g_live_key_ids.clear();
	for (uint32_t i = 0; i < (uint32_t)g_live_keys.size(); i++)
		g_live_key_ids.emplace(g_live_keys[i], i);
	g_live_key_gen++;
	// Most of them may still be in use: then wait until as many again have come.
	g_live_compact_at = (std::max)((size_t)1u << 18, g_live_keys.size() * 2);
}

struct LiveLine { LiveKey key; uint32_t draws, instances; };

// Collapses a frame into one line per distinct mesh/shader combination (two regions of a frame can
// still come to the same line).
static void live_lines(const Capture& c, std::vector<LiveLine>* out, uint32_t* game_draws)
{
	typedef LiveLine Line;
	std::vector<Line>& lines = *out;
	typedef std::tuple<uint8_t, uint8_t, int32_t, int32_t, uint32_t, uint32_t, uint32_t, int32_t, int32_t> Key;
	std::map<Key, size_t> index;
	for (const DrawRec& d : c.draws) {
		if (d.caller.origin != ORIGIN_XXMI)
			(*game_draws)++;
		Key k(d.type, d.caller.origin, d.ib_region, d.ib_res, d.a.index_count, d.a.first_index,
		      d.a.vertex_count, d.shaders[ST_VS], d.shaders[ST_PS]);
		auto it = index.find(k);
		uint32_t inst = d.a.instance_count ? d.a.instance_count : 1;
		if (it != index.end()) {
			lines[it->second].draws++;
			lines[it->second].instances += inst;
			continue;
		}
		Line l;
		memset(&l, 0, sizeof(l));
		LiveKey& li = l.key;
		li.type = d.type;
		li.origin = d.caller.origin;
		li.index_count = d.a.index_count;
		li.first_index = d.a.first_index;
		li.vertex_count = d.a.vertex_count;
		li.ib_state = 0xff;
		if (d.ib_region >= 0) {
			const CapRegion& r = c.regions[d.ib_region];
			li.ib_state = r.state;
			li.has_ib_hash = (r.state == RS_OK || r.state == RS_CACHED);
			li.ib_hash = li.has_ib_hash ? r.hash : 0;
			if (r.state == RS_CACHED)
				li.ib_state = RS_OK;     // the same line whether the hash was read back or remembered
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
		l.draws = 1;
		l.instances = inst;
		index.emplace(k, lines.size());
		lines.push_back(l);
	}
}

static void live_push(const Capture& c)
{
	if (!g_live_on)
		return;                          // a frame only the trigger or a recording looked at
	LiveFrame f;
	f.frame = c.frame;
	f.t_ms = c.t_end;
	f.draws = (uint32_t)c.draws.size();
	f.volatile_skips = c.volatile_skips;
	f.cost_ms = c.record_ms + c.finalize_ms;
	f.arena_bytes = c.arena_bytes;
	std::vector<LiveLine> lines;
	live_lines(c, &lines, &f.game_draws);

	EnterCriticalSection(&g_lock);
	// Stop/disconnect can race both GPU readback and the summary construction.
	// Check while committing, so an old frame cannot repopulate a cleared ring
	// even if a new live session has already started.
	if (!g_live_on || c.live_generation != g_live_generation) {
		LeaveCriticalSection(&g_lock);
		return;
	}
	std::unordered_map<uint32_t, size_t> at;     // two regions of a frame can come to the same line
	f.items.reserve(lines.size());
	for (const LiveLine& l : lines) {
		uint32_t id = live_key_id_locked(l.key);
		auto it = at.find(id);
		if (it != at.end()) {
			f.items[it->second].draws += l.draws;
			f.items[it->second].instances += l.instances;
			continue;
		}
		at.emplace(id, f.items.size());
		f.items.push_back({ id, l.draws, l.instances });
	}
	g_live_ring.push_back(std::move(f));
	uint32_t keep = g_live.keep_frames ? g_live.keep_frames : 1;
	while (g_live_ring.size() > keep)
		g_live_ring.pop_front();
	live_compact_keys_locked();
	LeaveCriticalSection(&g_lock);
}

void cap_live_configure(const LiveConfig& cfg)
{
	EnterCriticalSection(&g_lock);
	g_live = cfg;
	if (g_live.keep_frames > 3600) g_live.keep_frames = 3600;
	if (!g_live.every) g_live.every = 1;
	if (!cfg.enable) {
		g_live_generation++;
		live_clear_locked();
	}
	InterlockedExchange(&g_live_on, cfg.enable ? 1 : 0);
	LeaveCriticalSection(&g_lock);
}

static const char* k_region_state[] = { "pending", "ok", "oob", "skipped", "ok", "volatile" };

// [type, origin, ib hash, ib state, ib resource hash, index count, first index, vertex count, vs, ps]
static void live_key_json(JsonW& w, const LiveKey& k)
{
	w.u(k.type);
	w.str(origin_name(k.origin));
	if (k.has_ib_hash) w.hex32(k.ib_hash); else w.null();
	if (k.ib_state == 0xff) w.null(); else w.str(k_region_state[k.ib_state]);
	if (k.has_ib_res) w.hex32(k.ib_res_hash); else w.null();
	w.u(k.index_count); w.u(k.first_index); w.u(k.vertex_count);
	if (k.has_vs) w.hex64(k.vs); else w.null();
	if (k.has_ps) w.hex64(k.ps); else w.null();
}

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
		for (const LiveEntry& e : f.items) {
			w.begin_arr();
			live_key_json(w, g_live_keys[e.key]);
			w.u(e.draws); w.u(e.instances);
			w.end_arr();
		}
		w.end_arr();
		w.end_obj();
	}
	LeaveCriticalSection(&g_lock);
	w.end_arr();
	*json += w.s;
}

// The first kept frame numbered `frame` or later.
static size_t live_find_locked(uint32_t frame)
{
	size_t lo = 0, hi = g_live_ring.size();
	while (lo < hi) {
		size_t mid = (lo + hi) / 2;
		if (g_live_ring[mid].frame < frame) lo = mid + 1;
		else hi = mid;
	}
	return lo;
}

struct LiveAgg {
	uint32_t present = 0, first = 0, last = 0, max_draws = 0, max_inst = 0, base = 0;
	uint64_t draws = 0;
};
struct LiveTick { uint32_t frame; uint64_t t_ms; uint32_t draws, game_draws, lines; };

// Adds up a stretch of the stream. The frames are gone through a few dozen at a time, each time
// under the lock, so that a long stretch does not hold up the render thread.
void cap_live_summary(const LiveQuery& q, std::string* json)
{
	uint32_t first = q.first, last = q.last, bfirst = q.base_first, blast = q.base_last;
	uint32_t ring_first = 0, ring_last = 0;
	size_t ring_n = 0;
	EnterCriticalSection(&g_lock);
	const uint32_t gen = g_live_key_gen;
	if (!g_live_ring.empty()) {
		ring_first = g_live_ring.front().frame;
		ring_last = g_live_ring.back().frame;
		ring_n = g_live_ring.size();
		if (q.seconds > 0) {
			const uint64_t t_end = g_live_ring.back().t_ms;
			const uint64_t span = (uint64_t)(q.seconds * 1000.0);
			size_t i = g_live_ring.size() - 1;
			while (i > 0 && t_end - g_live_ring[i - 1].t_ms <= span)
				i--;
			first = g_live_ring[i].frame;
			last = ring_last;
			bfirst = blast = 0;
			if (q.base_seconds > 0 && i > 0) {
				const uint64_t t0 = g_live_ring[i].t_ms;
				const uint64_t bspan = (uint64_t)(q.base_seconds * 1000.0);
				size_t j = i;
				while (j > 0 && t0 - g_live_ring[j - 1].t_ms <= bspan)
					j--;
				if (j < i) {
					bfirst = g_live_ring[j].frame;
					blast = g_live_ring[i - 1].frame;
				}
			}
		}
	}
	LeaveCriticalSection(&g_lock);
	if (!first) first = ring_first;
	if (!last) last = ring_last;

	std::unordered_map<uint32_t, LiveAgg> agg;
	std::vector<LiveTick> ticks;
	uint32_t n_window = 0, n_base = 0;
	uint64_t t_first = 0, t_last = 0;
	bool stale = false;
	auto pass = [&](uint32_t from, uint32_t to, bool window) {
		uint32_t next = from;
		for (;;) {
			EnterCriticalSection(&g_lock);
			if (g_live_key_gen != gen) {
				stale = true;
				LeaveCriticalSection(&g_lock);
				return;
			}
			size_t i = live_find_locked(next);
			bool end = false;
			for (int n = 0; n < 64; n++, i++) {
				if (i >= g_live_ring.size() || g_live_ring[i].frame > to) {
					end = true;
					break;
				}
				const LiveFrame& f = g_live_ring[i];
				if (window) {
					if (!n_window++)
						t_first = f.t_ms;
					t_last = f.t_ms;
					if (q.timeline && ticks.size() < 20000)
						ticks.push_back({ f.frame, f.t_ms, f.draws, f.game_draws, (uint32_t)f.items.size() });
					for (const LiveEntry& e : f.items) {
						LiveAgg& a = agg[e.key];
						if (!a.present++)
							a.first = f.frame;
						a.last = f.frame;
						if (e.draws > a.max_draws) a.max_draws = e.draws;
						if (e.instances > a.max_inst) a.max_inst = e.instances;
						a.draws += e.draws;
					}
				} else {
					n_base++;
					for (const LiveEntry& e : f.items)
						agg[e.key].base++;
				}
				next = f.frame + 1;
			}
			LeaveCriticalSection(&g_lock);
			if (end)
				return;
		}
	};
	if (ring_n && bfirst && blast >= bfirst)
		pass(bfirst, blast, false);
	if (ring_n && !stale && last >= first)
		pass(first, last, true);

	JsonW w;
	w.begin_obj();
	w.key("ring"); w.begin_obj();
	w.ku("first", ring_first); w.ku("last", ring_last); w.ku("frames", (uint64_t)ring_n);
	w.end_obj();
	w.key("window"); w.begin_obj();
	w.ku("first", first); w.ku("last", last); w.ku("frames", n_window);
	w.ku("t_first", t_first); w.ku("t_last", t_last);
	w.end_obj();
	w.key("baseline"); w.begin_obj();
	w.ku("first", bfirst); w.ku("last", blast); w.ku("frames", n_base);
	w.end_obj();
	// [type, origin, ib hash, ib state, ib resource hash, index count, first index, vertex count, vs, ps,
	//  frames drawn in, first frame, last frame, most draws, most instances, all draws, baseline frames]
	w.key("items"); w.begin_arr();
	EnterCriticalSection(&g_lock);
	if (g_live_key_gen != gen)
		stale = true;
	if (!stale) {
		for (const auto& kv : agg) {
			const LiveAgg& a = kv.second;
			if (!a.present)
				continue;
			w.begin_arr();
			live_key_json(w, g_live_keys[kv.first]);
			w.u(a.present); w.u(a.first); w.u(a.last); w.u(a.max_draws); w.u(a.max_inst);
			w.u(a.draws); w.u(a.base);
			w.end_arr();
		}
	}
	LeaveCriticalSection(&g_lock);
	w.end_arr();
	w.kb("stale", stale);
	if (q.timeline) {
		// [frame, t_ms, draws, game draws, lines]
		w.key("timeline"); w.begin_arr();
		for (const LiveTick& t : ticks) {
			w.begin_arr();
			w.u(t.frame); w.u(t.t_ms); w.u(t.draws); w.u(t.game_draws); w.u(t.lines);
			w.end_arr();
		}
		w.end_arr();
	}
	w.end_obj();
	*json += w.s;
}

// ================================================================ frame boundary
static void release_capture_refs(Capture* c)
{
	for (IUnknown* u : c->held)
		IUnknown_Release(u);
	c->held.clear();
	rt_release_copies(c);
	rt_drop_pending(c);
	cont_release(c);
}

static std::deque<std::shared_ptr<Capture>> g_finalizing;   // recorded, waiting for the GPU

static void end_recording()
{
	std::shared_ptr<Capture> c = g_rec;
	InterlockedExchange(&g_recording, 0);
	InterlockedExchange(&g_caller_late, 0);
	InterlockedExchange(&g_events, 0);
	g_rec.reset();
	c->out_after.clear();
	c->t_end = now_ms();
	c->t_end_qpc = qpc_ms();
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
					g_rcache[p.key] = { r.hash, p.epoch, p.gen, p.serial };
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

// ================================================================ recording
static void rec_run_line(std::string* out, uint32_t id, const RRun& r)
{
	char b[160];
	snprintf(b, sizeof(b), "[%u,%u,%u,%u,%u,%u,%llu]\n", id, r.first, r.last, r.frames, r.max_draws, r.max_inst,
	         (unsigned long long)r.draws);
	*out += b;
}

static uint32_t rec_t_ms(double qpc) { return qpc > r_t0 ? (uint32_t)(qpc - r_t0) : 0; }

// What has piled up goes to the writer: new lines first, so that every run's line is known.
static void rec_hand_over()
{
	if (!r_lines_out.empty()) rec_lines(r_id, std::move(r_lines_out));
	if (!r_runs_out.empty()) rec_runs(r_id, std::move(r_runs_out));
	if (!r_frames_out.empty()) rec_frames(r_id, std::move(r_frames_out));
	r_lines_out.clear();
	r_runs_out.clear();
	r_frames_out.clear();
}

static void record_start(const char* how)
{
	int w = 0, h = 0;
	hash_get_resolution(&w, &h);
	r_id = rec_open(how, (uint32_t)g_frame_no, w, h);
	r_start_frame = (uint32_t)g_frame_no;
	r_t0 = qpc_ms();
	r_last_kf = 0;
	r_last_checkpoint = r_t0;
	r_first_kf = true;
	r_kf_stopped = false;
	r_news_frame = r_news = r_keyframes = r_runs_done = r_frames = 0;
	r_ids.clear();
	r_runs.clear();
	r_open.clear();
	r_pairs.clear();
	r_lines_out.clear();
	r_runs_out.clear();
	r_frames_out.clear();
	EnterCriticalSection(&g_lock);
	g_rs = RecStatus();
	g_rs.id = r_id;
	g_rs.start_frame = r_start_frame;
	g_rs.start_ms = now_ms();
	g_rs.started_by = how;
	InterlockedExchange(&g_record_on, 1);
	if (!g_tracking) {
		// Without a client nothing tracked the game's writes so far; from the next frame on it does.
		InterlockedExchange(&g_tracking, 1);
		InterlockedIncrement(&g_client_gen);
	}
	LeaveCriticalSection(&g_lock);
	if (rec_config().beep)
		MessageBeep(MB_ICONASTERISK);
	plog("record: recording %u started by %s at frame %u", r_id, how, r_start_frame);
}

static void record_stop(const char* why)
{
	for (uint32_t id : r_open) {
		RRun& r = r_runs[id];
		if (r.open) {
			rec_run_line(&r_runs_out, id, r);
			r.open = false;
			r_runs_done++;
		}
	}
	r_open.clear();
	rec_hand_over();
	const double dur = qpc_ms() - r_t0;
	EnterCriticalSection(&g_lock);
	g_rs.stop_frame = (uint32_t)g_frame_no;
	g_rs.stop_ms = now_ms();
	g_rs.stopped_by = why;
	g_rs.runs = r_runs_done;
	JsonW w;
	w.begin_obj();
	w.ks("stopped_by", why);
	w.ku("stop_frame", g_rs.stop_frame);
	w.ku("duration_ms", (uint64_t)dur);
	w.ku("frames", g_rs.frames);
	w.ku("lines", g_rs.lines);
	w.ku("runs", g_rs.runs);
	w.ku("keyframes", g_rs.keyframes);
	w.ku("news", r_news);
	w.kb("keyframes_stopped", r_kf_stopped);
	w.end_obj();
	InterlockedExchange(&g_record_on, 0);
	if (!g_client_on && g_tracking) {
		InterlockedExchange(&g_tracking, 0);
		InterlockedIncrement(&g_client_gen);
	}
	LeaveCriticalSection(&g_lock);
	rec_close(r_id, w.s.substr(1, w.s.size() - 2));
	if (rec_config().beep)
		MessageBeep(MB_OK);
	plog("record: recording %u stopped (%s) after %.1f s", r_id, why, dur / 1000.0);
}

// The runs not finished yet, so that a recording the game ended in the middle of still has them.
static void record_checkpoint()
{
	JsonW w;
	w.begin_obj();
	w.ku("frame", (uint32_t)g_frame_no);
	w.key("runs"); w.begin_arr();
	for (uint32_t id : r_open) {
		const RRun& r = r_runs[id];
		w.begin_arr();
		w.u(id); w.u(r.first); w.u(r.last); w.u(r.frames); w.u(r.max_draws); w.u(r.max_inst); w.u(r.draws);
		w.end_arr();
	}
	w.end_arr();
	w.end_obj();
	rec_hand_over();
	rec_open_runs(r_id, std::move(w.s));
}

// A frame of the recording's stream: its lines continue the runs they are in, start runs, and end
// the runs of lines it no longer draws.
static void record_push(const Capture& c)
{
	if (!g_record_on || !c.rec || c.rec_id != r_id)
		return;
	std::vector<LiveLine> lines;
	uint32_t game_draws = 0;
	live_lines(c, &lines, &game_draws);
	const uint32_t frame = c.frame;
	std::vector<uint32_t> present;
	present.reserve(lines.size());
	for (const LiveLine& l : lines) {
		// Index data without a hash is the game's CPU-built batches, drawn from another place in their
		// buffer every frame: by range they would be a new line in every frame.
		LiveKey key = l.key;
		if (!key.has_ib_hash)
			key.first_index = 0;
		auto it = r_ids.find(key);
		uint32_t id;
		if (it == r_ids.end()) {
			id = (uint32_t)r_runs.size();
			r_ids.emplace(key, id);
			r_runs.emplace_back();
			JsonW w;
			w.begin_obj();
			w.ku("id", id);
			w.ku("frame", frame);
			w.key("line"); w.begin_arr(); live_key_json(w, key); w.end_arr();
			w.end_obj();
			r_lines_out += w.s;
			r_lines_out += '\n';
		} else {
			id = it->second;
		}
		RRun& r = r_runs[id];
		if (r.seen != frame) {
			r.seen = frame;
			r.cur_draws = r.cur_inst = 0;
			present.push_back(id);
		}
		r.cur_draws += l.draws;
		r.cur_inst += l.instances;
	}
	for (uint32_t id : present) {
		RRun& r = r_runs[id];
		if (!r.open) {
			r.open = true;
			r.first = frame;
			r.frames = r.max_draws = r.max_inst = 0;
			r.draws = 0;
		}
		r.last = frame;
		r.frames++;
		if (r.cur_draws > r.max_draws) r.max_draws = r.cur_draws;
		if (r.cur_inst > r.max_inst) r.max_inst = r.cur_inst;
		r.draws += r.cur_draws;
	}
	for (uint32_t id : r_open) {
		RRun& r = r_runs[id];
		if (r.open && r.seen != frame) {
			rec_run_line(&r_runs_out, id, r);
			r.open = false;
			r_runs_done++;
		}
	}
	r_open.swap(present);
	const uint32_t rec[5] = { frame, rec_t_ms(c.t_end_qpc), (uint32_t)c.draws.size(), game_draws, (uint32_t)r_open.size() };
	const uint8_t* b = (const uint8_t*)rec;
	r_frames_out.insert(r_frames_out.end(), b, b + sizeof(rec));
	r_frames++;
	if (r_frames_out.size() >= 120 * sizeof(rec))
		rec_hand_over();
	EnterCriticalSection(&g_lock);
	g_rs.frames = r_frames;
	g_rs.lines = (uint32_t)r_runs.size();
	g_rs.runs = r_runs_done;
	g_rs.news = r_news;
	LeaveCriticalSection(&g_lock);
}

static void record_keyframe(std::shared_ptr<Capture> c)
{
	if (!g_record_on || c->rec_id != r_id) {
		release_capture_refs(c.get());
		return;
	}
	rec_hand_over();                     // the stream up to here first
	rec_keyframe(r_id, c, c->rec_reason, c->rec_what, rec_t_ms(c->t_end_qpc));
	EnterCriticalSection(&g_lock);
	g_rs.keyframes++;
	g_rs.last_keyframe = c->rec_reason;
	LeaveCriticalSection(&g_lock);
}

static bool key_held(uint32_t vk) { return (GetAsyncKeyState((int)vk) & 0x8000) != 0; }

static bool game_in_front()
{
	HWND fg = GetForegroundWindow();
	DWORD pid = 0;
	if (fg)
		GetWindowThreadProcessId(fg, &pid);
	return pid == GetCurrentProcessId();
}

// The recording at a frame boundary: the hotkey and the client's command, the limits, the open runs
// kept on disk now and then. Returns true when the frame starting now is to be a keyframe
// (r_kf_reason says why; record_kf_started is called if it does start).
static bool record_boundary()
{
	EnterCriticalSection(&g_lock);
	int cmd = g_rec_cmd;
	g_rec_cmd = RCMD_NONE;
	LeaveCriticalSection(&g_lock);
	const char* how = "client";
	const RecordConfig& rc = rec_config();
	if (rc.hotkey) {
		// Exactly the configured modifiers, and only while the game is in front: typing elsewhere
		// does nothing.
		const bool down = key_held(rc.key) && key_held(VK_CONTROL) == rc.ctrl && key_held(VK_SHIFT) == rc.shift &&
		                  key_held(VK_MENU) == rc.alt && game_in_front();
		if (down && !r_key_down) {
			cmd = g_record_on ? RCMD_STOP : RCMD_START;
			how = "hotkey";
		}
		r_key_down = down;
	}
	if (cmd == RCMD_START && !g_record_on)
		record_start(how);
	else if (cmd == RCMD_STOP && g_record_on)
		record_stop(how);
	if (!g_record_on)
		return false;

	const double now = qpc_ms();
	if (rc.max_minutes && now - r_t0 > rc.max_minutes * 60000.0) {
		record_stop("time limit");
		return false;
	}
	if (now - r_last_checkpoint > 5000) {
		r_last_checkpoint = now;
		record_checkpoint();
	}
	if (r_news_frame && (uint32_t)g_frame_no - r_news_frame > 30)
		r_news_frame = 0;                // too late to show it
	if (r_kf_stopped)
		return false;
	if (r_keyframes >= rc.max_keyframes || rec_bytes(r_id) >= ((uint64_t)rc.disk_mb << 20)) {
		r_kf_stopped = true;
		EnterCriticalSection(&g_lock);
		g_rs.keyframes_stopped = true;
		LeaveCriticalSection(&g_lock);
		plog("record: no more keyframes in recording %u (%u taken, %llu bytes written)", r_id, r_keyframes,
		     (unsigned long long)rec_bytes(r_id));
		return false;
	}
	if (r_first_kf) {
		r_kf_reason = "start";
		r_kf_what.clear();
	} else if (r_news_frame && now - r_last_kf >= rc.keyframe_gap_ms) {
		r_kf_reason = "new shader pair";
		r_kf_what = r_news_what;
	} else if (rc.keyframe_every_s && now - r_last_kf >= rc.keyframe_every_s * 1000.0) {
		r_kf_reason = "periodic";
		r_kf_what.clear();
	} else {
		return false;
	}
	return true;
}

static void record_kf_started()
{
	r_first_kf = false;
	r_last_kf = qpc_ms();
	r_keyframes++;
	if (!strcmp(r_kf_reason, "new shader pair"))
		r_news_frame = 0;
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

	if (c->slots) {
		slot_push(*c);
		return;
	}
	if (c->live) {
		trig_check_late(c.get());
		record_push(*c);
		live_push(*c);
		return;
	}
	if (c->rec_kf) {
		record_keyframe(c);
		return;
	}
	if (c->trig) {
		EnterCriticalSection(&g_lock);
		bool keep = g_trig_state == TRIG_RECORDING && g_trig_id == c->trig_id;
		if (keep) {
			g_trig_result = c;
			g_trig_state = TRIG_DONE;
		}
		LeaveCriticalSection(&g_lock);
		if (t_phase == TRIG_RECORDING && t_id == c->trig_id)
			t_phase = TRIG_OFF;
		if (!keep) {
			release_capture_refs(c.get());   // cancelled meanwhile
			return;
		}
		if (g_last)
			release_capture_refs(g_last.get());
		g_last = c;
		g_last_touch = now_ms();
		SetEvent(g_trig_event);
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
		bool block = c->wait_frames >= (c->live || c->slots ? 60u : 20u);
		if (!try_readback(c.get(), block)) {
			c->wait_frames++;
			break;
		}
		g_finalizing.pop_front();
		complete_capture(c);
	}
}

// `beside`: a frame recorded for no client (a keyframe of a recording), which leaves the client's last
// capture and the contents it keeps alone.
static void begin_recording(const CaptureRequest& rq, bool live, bool hash_volatile, uint64_t live_generation,
                            bool slots = false, bool beside = false)
{
	auto c = std::make_shared<Capture>();
	c->rq = rq;
	c->live = live;
	c->slots = slots;
	c->live_generation = live_generation;
	c->hash_volatile = hash_volatile;
	c->frame = (uint32_t)g_frame_no;
	c->t_start = now_ms();
	hash_get_resolution(&c->res_w, &c->res_h);
	if (!slots) {
		c->draws.reserve(8192);
		c->res.reserve(4096);
	}
	if (!live && !slots && !beside) {
		// One capture at a time has its contents kept: those of the one before end here.
		if (g_bar)
			cont_release(g_bar);
		if (rq.contents()) {
			c->rq.hold = true;           // what is read in place has to stay alive
			g_bar = c.get();
			g_mapped.clear();
			g_om_dirty = g_cs_dirty = true;
			InterlockedExchange(&g_barrier, 1);
		}
		InterlockedExchange(&g_events, rq.ct_events ? 1 : 0);
	}
	g_rec = c;
	InterlockedExchange(&g_caller_late, slots ? 1 : 0);
	InterlockedExchange(&g_recording, 1);
}

// The swap chain's buffer is not the game's to keep once it has been presented.
static void cont_before_present()
{
	Capture* c = g_bar;
	if (!c || c->tracks.empty() || !g_swap)
		return;
	ID3D11Resource* back = nullptr;
	IDXGISwapChain* sc = (IDXGISwapChain*)g_swap->obj;
	if (FAILED(((IDXGISwapChainVtbl*)g_swap->orig)->GetBuffer(sc, 0, IID_ID3D11Resource, (void**)&back)) || !back)
		return;
	auto it = c->tracks.find(back);
	if (it != c->tracks.end() && !it->second.buffer)
		cont_texture_write(c, it->second, back, 0, 0, 0);
	ID3D11Resource_Release(back);
}

// ---------------------------------------------------------------- jobs
// Reads requested by the client. Like the frame captures they are split in two:
// the copy is issued on one frame and collected on a later one.
struct Job {
	// 1 read buffer, 2 read texture, 3 release, 4 read a render target copy,
	// 5 release the render target copies of a capture, 6 read contents,
	// 7 release the contents of a capture
	int      type = 0;
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
	UINT     mw = 0, mh = 0, md = 1;
	uint8_t  tdim = D3D11_RESOURCE_DIMENSION_TEXTURE2D;
	UINT     tex_depth = 0;      // of a 3D texture
	bool     resolved = false;   // the samples of a multisampled texture, resolved

	// contents
	bool     c_buffer = false;   // put together from a buffer and the pieces kept of it
	bool     c_base = false;     // ... of which the buffer itself is needed
	bool     c_exact = true;
	const char* c_from = nullptr;
	struct Piece { ID3D11Buffer* chunk; uint32_t at, off, size; };
	std::vector<Piece> c_pieces; // laid over the buffer in this order

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
	ResInfo now;
	if (cr->known && cr->epoch && reg_write_hist(cr->ptr, &hist)) {
		j->unchanged_known = true;
		// Still the object that was captured, and nothing has written to this range since.
		j->unchanged = reg_get_resource(cr->ptr, &now) && now.serial == cr->serial
		            && hist_intact(hist, (uint32_t)off, (uint32_t)size, cr->epoch);
	}
	return true;
}

static const char* k_cont_why[] = {
	"", "", "the copy budget of the capture was used up", "the machine was short of free memory",
	"the copy could not be made", "this kind of texture cannot be read back (multisampled depth, or multisampled "
	"and typeless without a view to say how to resolve it)", "the resource was mapped at that moment",
};

// What a job that reads a texture reports about it.
static void job_texture_shape(Job* j, const CapRes& cr, const Staged& st)
{
	memset(&j->tdesc, 0, sizeof(j->tdesc));
	j->tdim = cr.dim;
	switch (cr.dim) {
	case D3D11_RESOURCE_DIMENSION_TEXTURE1D:
		j->tdesc.Width = cr.live.t1.Width;
		j->tdesc.Height = 1;
		j->tdesc.MipLevels = cr.live.t1.MipLevels;
		j->tdesc.ArraySize = cr.live.t1.ArraySize;
		break;
	case D3D11_RESOURCE_DIMENSION_TEXTURE3D:
		j->tdesc.Width = cr.live.t3.Width;
		j->tdesc.Height = cr.live.t3.Height;
		j->tdesc.MipLevels = cr.live.t3.MipLevels;
		j->tdesc.ArraySize = 1;
		j->tex_depth = cr.live.t3.Depth;
		break;
	default:
		j->tdesc = cr.live.t2;
		break;
	}
	j->tdesc.Format = st.format;
	j->mw = st.w; j->mh = st.h; j->md = st.d;
	j->resolved = st.resolved;
}

static bool start_read_texture(Job* j)
{
	ReadResult* o = j->out.get();
	Capture* c = g_last.get();
	if (!c || c->frame != j->frame) { o->err = "that capture is no longer held by the probe; capture again"; return false; }
	if (!c->rq.hold || c->held.empty()) { o->err = "that capture was taken without hold, its resources cannot be read"; return false; }
	if (j->res >= c->res.size()) { o->err = "bad resource id"; return false; }
	const CapRes* cr = &c->res[j->res];
	if (cr->dim != D3D11_RESOURCE_DIMENSION_TEXTURE1D && cr->dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D
	    && cr->dim != D3D11_RESOURCE_DIMENSION_TEXTURE3D) {
		o->err = "resource is not a texture";
		return false;
	}
	const uint32_t slices = cr->dim == D3D11_RESOURCE_DIMENSION_TEXTURE2D ? cr->live.t2.ArraySize
	                      : cr->dim == D3D11_RESOURCE_DIMENSION_TEXTURE1D ? cr->live.t1.ArraySize : 1;
	if (j->mip >= mips_of(*cr) || j->slice >= slices) { o->err = "mip or slice out of range"; return false; }
	Staged st;
	const int why = stage_subresource((ID3D11Resource*)cr->ptr, cr->live, cr->dim, j->mip, j->slice,
	                                  DXGI_FORMAT_UNKNOWN, 0, &st);
	if (why) {
		o->err = why == CS_FAILED ? "could not create staging texture" : k_cont_why[why];
		return false;
	}
	j->staging = st.tex;
	job_texture_shape(j, *cr, st);
	return true;
}

// A render target copy made while the frame was recorded: only the read-back is left to do.
static bool start_read_rt(Job* j)
{
	ReadResult* o = j->out.get();
	Capture* c = g_last.get();
	if (!c || c->frame != j->frame) { o->err = "that capture is no longer held by the probe; capture again"; return false; }
	if (j->res >= c->rts.size()) { o->err = "bad snapshot id"; return false; }
	const RtSnap& s = c->rts[j->res];
	if (s.state != RT_OK) { o->err = "that snapshot holds no pixels"; return false; }
	if (!s.staging) { o->err = "the copies of that capture have been released"; return false; }
	ID3D11Texture2D_AddRef(s.staging);
	j->staging = (ID3D11Resource*)s.staging;
	j->tdesc = s.desc;
	j->tdesc.Format = s.format;
	j->mw = s.w; j->mh = s.h;
	j->mip = s.mip; j->slice = s.slice;
	return true;
}

// What a slot held when its call was issued. A texture is its copy, or the
// texture itself when nothing has written to it since. A buffer is the buffer
// as it is now with the pieces laid over it that were set aside before each
// later write: for every byte, the oldest piece that is newer than the call.
static bool start_read_content(Job* j)
{
	ReadResult* o = j->out.get();
	Capture* c = g_last.get();
	if (!c || c->frame != j->frame) { o->err = "that capture is no longer held by the probe; capture again"; return false; }
	if (c->ct_released) {
		o->err = "the contents of that capture have been released (a newer capture was started, or they were let go of); capture again";
		return false;
	}
	if (j->res >= c->conts.size()) { o->err = "bad contents id"; return false; }
	const ContSnap& s = c->conts[j->res];
	const CapRes& cr = c->res[s.res];
	static const char* not_kept = "what this slot held at the call was not kept: ";
	if (s.tex) {
		Staged st;
		if (s.state == CS_COPIED) {
			ID3D11Resource_AddRef(s.copy);
			j->staging = s.copy;
			st.format = s.format; st.w = s.w; st.h = s.h; st.d = s.d; st.resolved = s.resolved;
			j->c_from = "copy";
		} else if (s.state != CS_LIVE) {
			o->err = std::string(not_kept) + k_cont_why[s.state];
			return false;
		} else {
			// Nothing has written to it since the call: it is read where it is.
			const int why = stage_subresource((ID3D11Resource*)cr.ptr, cr.live, cr.dim, s.mip, s.slice, s.hint, 0, &st);
			if (why) { o->err = std::string("could not be read: ") + k_cont_why[why]; return false; }
			j->staging = st.tex;
			j->c_from = "in place";
			j->c_exact = !c->ct_unknown;
		}
		job_texture_shape(j, cr, st);
		j->mip = s.mip;
		j->slice = s.slice;
		return true;
	}

	auto it = c->tracks.find(cr.ptr);
	if (it == c->tracks.end()) { o->err = "the contents of that capture are gone"; return false; }
	const ContTrack& t = it->second;
	if (t.lost && s.ver <= t.lost_ver) { o->err = std::string(not_kept) + k_cont_why[t.lost_why]; return false; }
	const uint64_t bw = cr.live.buf.ByteWidth;
	uint64_t off = j->offset, size = j->size;
	if (off >= bw) { o->err = "offset is past the end of the buffer"; return false; }
	if (!size || size > bw - off) size = bw - off;
	if (size > 512ull * 1024 * 1024) { o->err = "read too large"; return false; }
	j->c_buffer = true;
	j->r_off = off; j->r_size = size; j->r_bw = bw;
	bool need_base = true;
	for (size_t i = t.patches.size(); i-- > 0;) {    // newest first, so that the oldest is laid on last
		const ContPatch& p = t.patches[i];
		if (p.ver < s.ver)
			break;                                   // older than the call: not about this version
		if ((uint64_t)p.off + p.size <= off || p.off >= off + size)
			continue;
		ID3D11Buffer* chunk = c->cchunks[p.chunk].buf;
		ID3D11Buffer_AddRef(chunk);
		j->c_pieces.push_back({ chunk, p.at, p.off, p.size });
		if (p.off <= off && (uint64_t)p.off + p.size >= off + size)
			need_base = false;                       // it holds every byte that was asked for
	}
	j->c_base = need_base;
	j->c_from = !need_base ? "copy" : j->c_pieces.empty() ? "in place" : "in place and copies";
	if (need_base) {
		if (g_mapped.count(cr.ptr)) { o->err = "the buffer is mapped by the game right now; ask again"; return false; }
		ID3D11Buffer* st = create_staging((uint32_t)size);
		if (!st) { o->err = "could not create staging buffer"; return false; }
		D3D11_BOX box = { (UINT)off, 0, 0, (UINT)(off + size), 1, 1 };
		OCTX(CopySubresourceRegion)(rctx(), (ID3D11Resource*)st, 0, 0, 0, 0, (ID3D11Resource*)cr.ptr, 0, &box);
		j->staging = (ID3D11Resource*)st;
		j->c_exact = !c->ct_unknown;
	}
	return true;
}

static void write_content_meta(JsonW& w, const Job* j)
{
	// exact: these are the bytes of the call. Not so only when a command list ran in between,
	// whose writes cannot be seen, and part of the answer was read where it lies.
	w.kb("exact", j->c_exact);
	if (!j->c_exact)
		w.ks("why", "a deferred command list was executed after the call; its writes cannot be seen");
	w.ks("from", j->c_from ? j->c_from : "");
}

// Returns true when the job is finished (successfully or not).
static bool finish_read_content_buffer(Job* j, bool block)
{
	ReadResult* o = j->out.get();
	const UINT flags = block ? 0 : D3D11_MAP_FLAG_DO_NOT_WAIT;
	D3D11_MAPPED_SUBRESOURCE m;
	o->data.assign((size_t)j->r_size, 0);
	if (j->c_base) {
		memset(&m, 0, sizeof(m));
		HRESULT hr = OCTX(Map)(rctx(), j->staging, 0, D3D11_MAP_READ, flags, &m);
		if (hr == DXGI_ERROR_WAS_STILL_DRAWING)
			return false;
		if (FAILED(hr) || !m.pData) { o->err = "Map failed"; o->data.clear(); return true; }
		memcpy(o->data.data(), m.pData, (size_t)j->r_size);
		OCTX(Unmap)(rctx(), j->staging, 0);
	}
	for (const Job::Piece& p : j->c_pieces) {
		memset(&m, 0, sizeof(m));
		HRESULT hr = OCTX(Map)(rctx(), (ID3D11Resource*)p.chunk, 0, D3D11_MAP_READ, flags, &m);
		if (hr == DXGI_ERROR_WAS_STILL_DRAWING)
			return false;                            // started over on a later frame
		if (FAILED(hr) || !m.pData) { o->err = "Map failed"; o->data.clear(); return true; }
		const uint64_t lo = std::max<uint64_t>(p.off, j->r_off);
		const uint64_t hi = std::min<uint64_t>((uint64_t)p.off + p.size, j->r_off + j->r_size);
		memcpy(o->data.data() + (lo - j->r_off), (const uint8_t*)m.pData + p.at + (lo - p.off), (size_t)(hi - lo));
		OCTX(Unmap)(rctx(), (ID3D11Resource*)p.chunk, 0);
	}
	JsonW w;
	w.begin_obj();
	w.ku("offset", j->r_off);
	w.ku("size", j->r_size);
	w.ku("byte_width", j->r_bw);
	w.ku("read_frame", (uint32_t)g_frame_no);
	w.ku("pieces", j->c_pieces.size());
	write_content_meta(w, j);
	w.end_obj();
	o->meta = w.s;
	o->ok = true;
	return true;
}

// Returns true when the job is finished (successfully or not).
static bool finish_read(Job* j, bool block)
{
	if (j->c_buffer)
		return finish_read_content_buffer(j, block);
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
			o->data.resize(row_bytes * rows * j->md);
			size_t copy = std::min(row_bytes, (size_t)m.RowPitch);
			for (size_t z = 0; z < j->md; z++)
				for (size_t r = 0; r < rows; r++)
					memcpy(o->data.data() + (z * rows + r) * row_bytes,
					       (const uint8_t*)m.pData + z * m.DepthPitch + r * m.RowPitch, copy);
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
			w.ks("kind", j->tdim == D3D11_RESOURCE_DIMENSION_TEXTURE1D ? "tex1d"
			           : j->tdim == D3D11_RESOURCE_DIMENSION_TEXTURE3D ? "tex3d" : "tex2d");
			if (j->tdim == D3D11_RESOURCE_DIMENSION_TEXTURE3D) {
				// The rows of every depth slice follow one another.
				w.ku("depth", j->md);
				w.ku("tex_depth", j->tex_depth);
			}
			if (j->resolved) {
				w.kb("resolved", true);
				w.ku("samples", d.SampleDesc.Count);
			}
			w.ku("read_frame", (uint32_t)g_frame_no);
			if (j->type == 6)
				write_content_meta(w, j);
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
	for (Job::Piece& p : j->c_pieces)
		ID3D11Buffer_Release(p.chunk);
	j->c_pieces.clear();
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
		case 4: flying = start_read_rt(j.get()); break;
		case 5:
			// Reads that are under way hold their own reference.
			if (g_last && g_last->frame == j->frame)
				rt_release_copies(g_last.get());
			break;
		case 6: flying = start_read_content(j.get()); break;
		case 7:
			if (g_last && g_last->frame == j->frame)
				cont_release(g_last.get());
			break;
		case 3:
			if (g_last) { release_capture_refs(g_last.get()); g_last.reset(); }
			arena_free_idle();
			break;
		}
		g_last_touch = now_ms();
		if (flying) {
			g_inflight.push_back(j);
		} else {
			drop_staging(j.get());       // what a start that gave up had already taken hold of
			SetEvent(j->done);
		}
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

// The trigger at a frame boundary: takes the client's command over, counts the baseline down,
// expires. Returns true when the frame starting now is the one to record for it.
static bool trig_boundary()
{
	bool due = false;
	EnterCriticalSection(&g_lock);
	const int cmd = g_trig_cmd;
	g_trig_cmd = TCMD_NONE;
	if (cmd == TCMD_ARM) {
		t_spec = g_trig_next;
		t_id = g_trig_id;
		t_ib.clear();
		t_shader.clear();
		t_pairs.clear();
		t_ib.insert(t_spec.ib.begin(), t_spec.ib.end());
		t_shader.insert(t_spec.shader.begin(), t_spec.shader.end());
		t_baseline_left = t_spec.new_pairs ? t_spec.baseline_frames : 0;
		t_phase = t_baseline_left ? TRIG_BASELINE : TRIG_ARMED;
		g_trig_state = t_phase;
		g_trig_armed_frame = (uint32_t)g_frame_no;
		g_trig_armed_ms = now_ms();
		g_trig_watched = 0;
	} else if (cmd == TCMD_CANCEL) {
		t_phase = TRIG_OFF;
		t_ib.clear();
		t_shader.clear();
		t_pairs.clear();
	}
	if (t_phase == TRIG_BASELINE || t_phase == TRIG_ARMED) {
		g_trig_watched++;
		if (t_spec.expire_ms && now_ms() - g_trig_armed_ms > t_spec.expire_ms) {
			t_phase = TRIG_OFF;
			if (g_trig_state == TRIG_BASELINE || g_trig_state == TRIG_ARMED) {
				g_trig_state = TRIG_EXPIRED;
				SetEvent(g_trig_event);
			}
		} else if (t_phase == TRIG_BASELINE && --t_baseline_left == 0) {
			t_phase = TRIG_ARMED;
			if (g_trig_state == TRIG_BASELINE)
				g_trig_state = TRIG_ARMED;
		}
	} else if (t_phase == TRIG_FIRED && g_trig_state == TRIG_FIRED) {
		due = (uint32_t)g_frame_no - t_fire_frame > t_spec.delay_frames;
	}
	InterlockedExchange(&g_trig_watch, (t_phase == TRIG_BASELINE || t_phase == TRIG_ARMED) ? 1 : 0);
	LeaveCriticalSection(&g_lock);
	return due;
}

void cap_on_frame_boundary()
{
	InterlockedIncrement(&g_frame_no);
	if (!g_ctx || !g_dev)
		return;
	hooks_verify();

	if (g_bar) {
		cont_before_present();
		g_om_dirty = g_cs_dirty = true;      // a flip leaves the back buffer unbound
		cont_publish(g_bar);
	}
	if (g_rec)
		end_recording();
	// Before the client check: starting or stopping a recording can switch write tracking.
	const bool kf_due = record_boundary();

	LONG gen = g_client_gen;
	if (gen != g_seen_client_gen) {
		// A client came or went: cached hashes cannot be trusted across the gap.
		g_seen_client_gen = gen;
		g_track_gen++;
		g_rcache.clear();
		if (g_bar && g_bar != g_rec.get())
			cont_release(g_bar);
		if (g_tracking)
			resync_gpu_writers();
		else
			cap_state_cleared();
		if (!g_client_on && g_last) { release_capture_refs(g_last.get()); g_last.reset(); }
	}

	process_finalizing();
	run_jobs();

	if (!g_tracking && g_finalizing.empty() && g_inflight.empty())
		arena_free_idle();

	// Do not pin the game's resources forever if the client went quiet.
	if (g_last && now_ms() - g_last_touch > 180000
	    && (!g_last->held.empty() || rt_holds_copies(g_last.get()) || !g_last->tracks.empty())) {
		release_capture_refs(g_last.get());
		plog("capture: released resources of frame %u after inactivity", g_last->frame);
	}

	const bool trig_due = trig_boundary();
	const bool watching = t_phase == TRIG_BASELINE || t_phase == TRIG_ARMED;
	const bool recording = g_record_on != 0;
	bool start = false, live = false, hash_volatile = false, slots = false, trig = false, kf = false;
	uint64_t live_generation = 0;
	CaptureRequest rq;
	EnterCriticalSection(&g_lock);
	if (g_req_state == REQ_PENDING && !g_rec) {
		// An explicit request takes the next frame; the stream resumes after it.
		rq = g_req;
		start = true;
		g_req_state = REQ_RECORDING;
	} else if (trig_due && !g_rec) {
		rq = t_spec.rq;
		start = trig = true;
		g_trig_state = TRIG_RECORDING;
		g_trig_capture_frame = (uint32_t)g_frame_no;
		t_phase = TRIG_RECORDING;
	} else if (kf_due && !g_rec && g_finalizing.size() < 8) {
		// A keyframe of the recording: the whole frame, kept on disk instead of for reading.
		rq = CaptureRequest();
		rq.detail = 1;
		rq.hash_ib = true;
		rq.hash_vb = true;
		rq.cb_data = rec_config().keyframe_cb;
		rq.hold = false;
		start = kf = true;
	} else if ((g_live_on || watching || recording) && g_tracking && !g_rec && g_finalizing.size() < 8 &&
	           (watching || recording || (uint32_t)g_frame_no % g_live.every == 0)) {
		// The stream's frames are also what the trigger watches and what is recorded: then every
		// frame is looked at.
		rq = CaptureRequest();
		rq.detail = -1;
		rq.hash_ib = g_live.hash_ib || watching || recording;
		rq.hash_vb = g_live.hash_vb;
		rq.cb_data = false;
		rq.hold = false;
		hash_volatile = g_live.hash_volatile;
		live_generation = g_live_generation;
		start = live = true;
	} else if (!g_rec && g_swap && g_finalizing.size() < 4 && slot_due((uint32_t)g_frame_no)) {
		// The slot cache: only draws that were not seen before are recorded in this frame.
		rq = CaptureRequest();
		rq.detail = -1;
		rq.hash_ib = true;
		rq.hash_vb = false;
		rq.cb_data = false;
		rq.hold = false;
		start = slots = true;
	}
	LeaveCriticalSection(&g_lock);
	if (start) {
		if (slots)
			slot_prepare();
		begin_recording(rq, live, hash_volatile, live_generation, slots, kf);
		if (trig) {
			g_rec->trig = true;
			g_rec->trig_id = t_id;
		} else if (kf) {
			g_rec->rec_kf = true;
			g_rec->rec_id = r_id;
			g_rec->rec_reason = r_kf_reason;
			g_rec->rec_what = r_kf_what;
			record_kf_started();
		} else if (live) {
			if (t_phase == TRIG_ARMED)
				g_rec->trig_id = t_id;       // watched for this arming: see trig_check_late
			if (recording) {
				g_rec->rec = true;
				g_rec->rec_id = r_id;
			}
		}
	}
}

// ================================================================ IPC side
void cap_target_selected()
{
	// No cross-device migration is allowed after the first swap chain binds.
	// Advancing this generation clears provisional caches and re-reads UAV/SO
	// bindings on the render thread, using the selected immediate context.
	InterlockedIncrement(&g_client_gen);
}

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
	if (g_trig_state == TRIG_FIRED || g_trig_state == TRIG_RECORDING) {
		// It would take the frame or release what the triggered capture keeps.
		LeaveCriticalSection(&g_lock);
		*err = "the trigger fired and its capture is being recorded: collect it with trigger_wait first";
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

// ---------------------------------------------------------------- trigger, the client's side
bool cap_trigger_arm(const TriggerSpec& spec, std::string* err)
{
	if (!g_ctx || !g_dev || !g_swap) {
		*err = "the probe has no device and swap chain to watch yet; is the game rendering?";
		return false;
	}
	if (!g_tracking) {
		*err = "no client is connected";
		return false;
	}
	EnterCriticalSection(&g_lock);
	if (g_trig_state == TRIG_FIRED || g_trig_state == TRIG_RECORDING) {
		LeaveCriticalSection(&g_lock);
		*err = "the trigger fired and its capture is being recorded: collect it with trigger_wait or cancel it first";
		return false;
	}
	g_trig_next = spec;
	g_trig_cmd = TCMD_ARM;
	g_trig_id++;
	g_trig_state = TRIG_ARMING;
	g_trig_fire = TrigFire();
	g_trig_fired_ms = 0;
	g_trig_capture_frame = 0;
	g_trig_watched = 0;
	g_trig_armed_ms = now_ms();
	g_trig_armed_frame = (uint32_t)g_frame_no;
	g_trig_result.reset();
	ResetEvent(g_trig_event);
	LeaveCriticalSection(&g_lock);
	return true;
}

void cap_trigger_cancel()
{
	EnterCriticalSection(&g_lock);
	trig_cancel_locked();
	LeaveCriticalSection(&g_lock);
}

static void trig_state_json_locked(JsonW& w)
{
	const uint64_t now = now_ms();
	w.begin_obj();
	w.ks("state", k_trig_state[g_trig_state]);
	w.ku("id", g_trig_id);
	w.ku("frame", (uint32_t)g_frame_no);
	if (g_trig_state != TRIG_OFF || g_trig_armed_ms) {
		w.ku("armed_frame", g_trig_armed_frame);
		w.ku("armed_ms_ago", now - g_trig_armed_ms);
		w.ku("watched_frames", g_trig_watched);
	}
	if (g_trig_fired_ms) {
		const TrigFire& f = g_trig_fire;
		w.key("fired"); w.begin_obj();
		w.ku("frame", f.frame);
		w.ku("draw", f.draw);
		w.ku("type", f.type);
		w.ks("origin", origin_name(f.origin));
		w.ku("count", f.count);
		w.ks("what", f.what);
		if (f.ib) w.kh32("ib", f.ib);
		if (f.shader) w.kh64("shader", f.shader);
		if (f.vs) w.kh64("vs", f.vs);
		if (f.ps) w.kh64("ps", f.ps);
		w.kb("late", f.late);
		w.ku("ms_ago", now - g_trig_fired_ms);
		w.end_obj();
	}
	if (g_trig_capture_frame)
		w.ku("capture_frame", g_trig_capture_frame);
	w.end_obj();
}

std::shared_ptr<Capture> cap_trigger_wait(uint32_t timeout_ms, std::string* state)
{
	EnterCriticalSection(&g_lock);
	const bool waiting = g_trig_state != TRIG_OFF && g_trig_state != TRIG_DONE && g_trig_state != TRIG_EXPIRED;
	LeaveCriticalSection(&g_lock);
	if (waiting && timeout_ms)
		WaitForSingleObject(g_trig_event, timeout_ms);
	std::shared_ptr<Capture> out;
	JsonW w;
	EnterCriticalSection(&g_lock);
	trig_state_json_locked(w);
	if (g_trig_state == TRIG_DONE) {
		out = g_trig_result;             // handed over once; it stays the latest capture for reads
		g_trig_result.reset();
		g_trig_state = TRIG_OFF;
	} else if (g_trig_state == TRIG_EXPIRED) {
		g_trig_state = TRIG_OFF;          // said once
	}
	LeaveCriticalSection(&g_lock);
	*state = w.s;
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

void cap_read_rt(uint32_t frame, uint32_t snap, uint32_t timeout_ms, ReadResult* out)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 4; j->frame = frame; j->res = snap;
	if (!run_job(j, timeout_ms)) {
		out->err = "timed out waiting for the game to present a frame";
		return;
	}
	*out = std::move(*j->out);
}

void cap_release_rt(uint32_t frame, uint32_t timeout_ms)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 5; j->frame = frame;
	run_job(j, timeout_ms);
}

void cap_read_content(uint32_t frame, uint32_t snap, uint64_t offset, uint64_t size, uint32_t timeout_ms, ReadResult* out)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 6; j->frame = frame; j->res = snap; j->offset = offset; j->size = size;
	if (!run_job(j, timeout_ms)) {
		out->err = "timed out waiting for the game to present a frame";
		return;
	}
	*out = std::move(*j->out);
}

void cap_release_content(uint32_t frame, uint32_t timeout_ms)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 7; j->frame = frame;
	run_job(j, timeout_ms);
}

void cap_release(uint32_t timeout_ms)
{
	auto j = std::make_shared<Job>();
	j->out = std::make_shared<ReadResult>();
	j->type = 3;
	run_job(j, timeout_ms);
}

void cap_record_request(bool start)
{
	EnterCriticalSection(&g_lock);
	g_rec_cmd = start ? RCMD_START : RCMD_STOP;
	LeaveCriticalSection(&g_lock);
}

bool cap_record_on() { return g_record_on != 0; }

void cap_record_status(std::string* out)
{
	JsonW w;
	w.begin_obj();
	EnterCriticalSection(&g_lock);
	const RecStatus s = g_rs;
	const bool on = g_record_on != 0;
	LeaveCriticalSection(&g_lock);
	w.kb("on", on);
	w.ks("hotkey", rec_config().hotkey ? rec_hotkey_name() : std::string());
	if (s.id) {
		w.ku("id", s.id);
		w.ks("started_by", s.started_by);
		w.ku("start_frame", s.start_frame);
		w.kd("seconds", ((on ? now_ms() : s.stop_ms) - s.start_ms) / 1000.0);
		w.ku("frames", s.frames);
		w.ku("lines", s.lines);
		w.ku("runs", s.runs);
		w.ku("keyframes", s.keyframes);
		w.ku("news", s.news);
		w.kb("keyframes_stopped", s.keyframes_stopped);
		if (!s.last_keyframe.empty()) w.ks("last_keyframe", s.last_keyframe);
		if (!on) {
			w.ks("stopped_by", s.stopped_by);
			w.ku("stop_frame", s.stop_frame);
		}
	}
	w.end_obj();
	std::string files;                   // the writer's: folder, error, bytes, pending, root
	rec_status_members(&files);
	if (!files.empty()) {
		w.s.pop_back();
		w.s += ',' + files + '}';
	}
	*out = w.s;
}

void cap_status_json(std::string* out)
{
	char b[384];
	snprintf(b, sizeof(b), "\"tracking\":%s,\"recording\":%s,\"live\":%s,\"region_cache\":%zu,\"arena_chunks\":%zu,"
	         "\"contents_kept\":%s,\"contents_frame\":%ld,\"contents_bytes\":%lld,\"contents_copies\":%ld,\"contents_lost\":%ld",
	         g_tracking ? "true" : "false", g_recording ? "true" : "false", g_live_on ? "true" : "false",
	         g_rcache.size(), g_chunks.size(), g_barrier ? "true" : "false", (long)g_kept_frame, (long long)g_kept_bytes,
	         (long)g_kept_copies, (long)g_kept_lost);
	*out += b;
	EnterCriticalSection(&g_lock);
	snprintf(b, sizeof(b), ",\"live_frames\":%zu,\"live_lines\":%zu,\"trigger\":\"%s\"",
	         g_live_ring.size(), g_live_keys.size(), k_trig_state[g_trig_state]);
	LeaveCriticalSection(&g_lock);
	*out += b;
	std::string rec;
	cap_record_status(&rec);
	*out += ",\"record\":" + rec;
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
		w.ku("cf", r.create_frame);
		w.ku("sn", r.serial);
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
	if (!c.rq.rt_match.empty()) {
		w.ku("rt_matched", c.rt_matched);
		w.ku("rt_draws", c.rt_draws);
		w.ku("rt_depth_only", c.rt_depth_only);
		w.ku("rt_bytes", c.rt_bytes);
		w.ku("rt_over_budget", c.rt_skipped);
	}
	if (c.rq.contents()) {
		// As they stand when the capture is handed over: copies are made as the game goes on writing.
		w.ku("ct_snaps", c.conts.size());
		w.ku("ct_copies", c.ct_copies);
		w.ku("ct_bytes", c.ct_bytes);
		w.ku("ct_lost", c.ct_lost);
		w.ku("ct_budget", c.rq.ct_budget);
		w.ku("ct_out_calls", c.ct_out_calls);
	}
	if (c.rq.ct_events) {
		w.ku("events", c.events.size());
		w.ku("events_dropped", c.events_dropped);
	}
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
			w.ku("cf", s.info.create_frame);
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

	// Fixed-function state objects, referred to by index from the draws.
	if (!c.states.empty()) {
		w.key("states"); w.begin_arr();
		for (const CapState& s : c.states) {
			w.begin_obj();
			switch (s.kind) {
			case SK_BLEND: {
				const D3D11_BLEND_DESC& b = s.d.blend;
				w.ks("k", "blend");
				w.kb("a2c", b.AlphaToCoverageEnable != 0);
				w.kb("ind", b.IndependentBlendEnable != 0);
				// [enable, src, dst, op, src alpha, dst alpha, op alpha, write mask] per target
				w.key("rt"); w.begin_arr();
				for (int i = 0; i < (b.IndependentBlendEnable ? 8 : 1); i++) {
					const D3D11_RENDER_TARGET_BLEND_DESC& t = b.RenderTarget[i];
					w.begin_arr();
					w.u(t.BlendEnable ? 1 : 0); w.u(t.SrcBlend); w.u(t.DestBlend); w.u(t.BlendOp);
					w.u(t.SrcBlendAlpha); w.u(t.DestBlendAlpha); w.u(t.BlendOpAlpha); w.u(t.RenderTargetWriteMask);
					w.end_arr();
				}
				w.end_arr();
				break;
			}
			case SK_DEPTH: {
				const D3D11_DEPTH_STENCIL_DESC& t = s.d.depth;
				w.ks("k", "depth");
				w.kb("de", t.DepthEnable != 0);
				w.ku("dw", t.DepthWriteMask);
				w.ku("df", t.DepthFunc);
				w.kb("se", t.StencilEnable != 0);
				w.ku("srm", t.StencilReadMask);
				w.ku("swm", t.StencilWriteMask);
				// [fail, depth fail, pass, func]
				w.key("ff"); w.begin_arr();
				w.u(t.FrontFace.StencilFailOp); w.u(t.FrontFace.StencilDepthFailOp); w.u(t.FrontFace.StencilPassOp); w.u(t.FrontFace.StencilFunc);
				w.end_arr();
				w.key("bf"); w.begin_arr();
				w.u(t.BackFace.StencilFailOp); w.u(t.BackFace.StencilDepthFailOp); w.u(t.BackFace.StencilPassOp); w.u(t.BackFace.StencilFunc);
				w.end_arr();
				break;
			}
			case SK_RASTER: {
				const D3D11_RASTERIZER_DESC& t = s.d.raster;
				w.ks("k", "raster");
				w.ku("fill", t.FillMode);
				w.ku("cull", t.CullMode);
				w.kb("ccw", t.FrontCounterClockwise != 0);
				w.ki("db", t.DepthBias);
				w.kd("dbc", t.DepthBiasClamp);
				w.kd("ssdb", t.SlopeScaledDepthBias);
				w.kb("dc", t.DepthClipEnable != 0);
				w.kb("sc", t.ScissorEnable != 0);
				w.kb("ms", t.MultisampleEnable != 0);
				w.kb("aal", t.AntialiasedLineEnable != 0);
				break;
			}
			default: {
				const D3D11_SAMPLER_DESC& t = s.d.sampler;
				w.ks("k", "sampler");
				w.ku("f", t.Filter);
				w.ku("u", t.AddressU); w.ku("v", t.AddressV); w.ku("w", t.AddressW);
				w.kd("lb", t.MipLODBias);
				w.ku("ma", t.MaxAnisotropy);
				w.ku("cf", t.ComparisonFunc);
				w.key("bc"); w.begin_arr();
				for (int i = 0; i < 4; i++) w.d(t.BorderColor[i]);
				w.end_arr();
				w.kd("minl", t.MinLOD);
				w.kd("maxl", t.MaxLOD);
				break;
			}
			}
			w.end_obj();
		}
		w.end_arr();
	}

	// [resource, kind (0 srv, 1 rtv, 2 dsv, 3 uav), format, view dimension, first mip or element,
	//  mip or element count, first slice, slice count, flags]
	if (!c.view_descs.empty()) {
		w.key("views"); w.begin_arr();
		for (const CapView& v : c.view_descs) {
			w.begin_arr();
			w.i(v.res); w.u(v.kind); w.u(v.format); w.u(v.dim);
			w.u(v.p[0]); w.u(v.p[1]); w.u(v.p[2]); w.u(v.p[3]); w.u(v.flags);
			w.end_arr();
		}
		w.end_arr();
	}

	// Copies of render targets around chosen draws; read with read_rt by position.
	if (!c.rq.rt_match.empty()) {
		static const char* k_rt_state[] = { "ok", "not_2d", "multisampled", "over_budget", "failed" };
		w.key("rts"); w.begin_arr();
		for (const RtSnap& s : c.rts) {
			w.begin_obj();
			w.ku("draw", s.draw);
			w.ku("when", s.when);
			w.ku("kind", s.kind);
			w.ku("slot", s.slot);
			w.ki("res", s.res);
			w.ks("state", k_rt_state[s.state]);
			if (s.state == RT_OK || s.state == RT_BUDGET) {
				w.ku("mip", s.mip); w.ku("slice", s.slice);
				w.ku("w", s.w); w.ku("h", s.h);
				w.ku("fmt", (uint32_t)s.format);
				w.ks("fmt_name", dxgi_format_name(s.format));
				w.ku("bytes", s.bytes);
				w.key("box"); w.begin_arr(); w.u(s.box[0]); w.u(s.box[1]); w.u(s.box[2]); w.u(s.box[3]); w.end_arr();
				w.kb("boxed", s.boxed);
			}
			w.end_obj();
		}
		w.end_arr();
	}

	// What calls saw, read with read_content by position: [resource, 0, version] is a buffer,
	// [resource, 1, mip, slice] one subresource of a texture.
	if (c.rq.contents()) {
		w.key("cont"); w.begin_arr();
		for (const ContSnap& s : c.conts) {
			w.begin_arr();
			w.i(s.res); w.u(s.tex);
			if (s.tex) { w.u(s.mip); w.u(s.slice); }
			else w.u(s.ver);
			w.end_arr();
		}
		w.end_arr();
	}

	// The writes between the calls: [kind, number of the call that follows, written resource,
	// source resource, four numbers] and for a clear its four values.
	if (c.rq.ct_events) {
		w.key("ev"); w.begin_arr();
		for (const CapEvent& e : c.events) {
			w.begin_arr();
			w.u(e.type); w.u(e.draw); w.i(e.dst); w.i(e.src);
			w.u(e.a[0]); w.u(e.a[1]); w.u(e.a[2]); w.u(e.a[3]);
			if (e.has_f) { w.d(e.f[0]); w.d(e.f[1]); w.d(e.f[2]); w.d(e.f[3]); }
			w.end_arr();
		}
		w.end_arr();
	}

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
		if (d.indirect_snap >= 0) w.ki("inds", d.indirect_snap);
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
		// What follows is additional: lists run parallel to "srv" and "out" so that older readers still work.
		if (d.srv_count && c.rq.detail >= 1) {
			w.key("srvv"); w.begin_arr();
			for (uint32_t k = 0; k < d.srv_count; k++) w.i(c.views[d.srv_begin + k].view);
			w.end_arr();
		}
		if (d.out_count) {
			w.key("outv"); w.begin_arr();
			for (uint32_t k = 0; k < d.out_count; k++) w.i(c.views[d.out_begin + k].view);
			w.end_arr();
		}
		if (d.smp_count) {
			w.key("smp"); w.begin_arr();
			for (uint32_t k = 0; k < d.smp_count; k++) {
				const SamplerBind& b = c.samplers[d.smp_begin + k];
				w.begin_arr(); w.u(b.stage); w.u(b.slot); w.i(b.state); w.end_arr();
			}
			w.end_arr();
		}
		if (d.has_state) {
			// [state, factor r g b a, sample mask]
			w.key("bs"); w.begin_arr();
			w.i(d.blend); w.d(d.blend_factor[0]); w.d(d.blend_factor[1]); w.d(d.blend_factor[2]); w.d(d.blend_factor[3]);
			w.u(d.sample_mask);
			w.end_arr();
			w.key("dss"); w.begin_arr(); w.i(d.depth); w.u(d.stencil_ref); w.end_arr();
			w.ki("rs", d.raster);
			if (d.sc_count) {
				w.key("sc"); w.begin_arr();
				for (uint32_t k = 0; k < d.sc_count; k++) {
					const D3D11_RECT& r = c.rects[d.sc_begin + k];
					w.begin_arr(); w.i(r.left); w.i(r.top); w.i(r.right); w.i(r.bottom); w.end_arr();
				}
				w.end_arr();
			}
			w.ku("vpn", d.vp_count);
			w.key("vpz"); w.begin_arr(); w.d(d.vp_z[0]); w.d(d.vp_z[1]); w.end_arr();
			if (d.vp_count > 1) {
				// every viewport: [left, top, width, height, min depth, max depth]
				w.key("vps"); w.begin_arr();
				for (uint32_t k = 0; k < d.vp_count; k++) {
					const D3D11_VIEWPORT& v = c.viewports[d.vp_begin + k];
					w.begin_arr();
					w.d(v.TopLeftX); w.d(v.TopLeftY); w.d(v.Width); w.d(v.Height); w.d(v.MinDepth); w.d(v.MaxDepth);
					w.end_arr();
				}
				w.end_arr();
			}
		}
		if (c.rq.contents()) {
			// Contents, by position in "cont": lists run parallel to "vb", "cb", "srv" and "out" (-1: none).
			// For an output it is what the call left there.
			bool any = false;
			if (d.ib_cont >= 0) w.ki("cib", d.ib_cont);
			for (uint32_t k = 0; k < d.vb_count; k++) any = any || c.vbs[d.vb_begin + k].cont >= 0;
			if (any) {
				w.key("cvb"); w.begin_arr();
				for (uint32_t k = 0; k < d.vb_count; k++) w.i(c.vbs[d.vb_begin + k].cont);
				w.end_arr();
			}
			any = false;
			for (uint32_t k = 0; k < d.cb_count; k++) any = any || c.cbs[d.cb_begin + k].cont >= 0;
			if (any) {
				w.key("ccb"); w.begin_arr();
				for (uint32_t k = 0; k < d.cb_count; k++) w.i(c.cbs[d.cb_begin + k].cont);
				w.end_arr();
			}
			any = false;
			for (uint32_t k = 0; k < d.srv_count; k++) any = any || c.views[d.srv_begin + k].cont >= 0;
			if (any) {
				w.key("csrv"); w.begin_arr();
				for (uint32_t k = 0; k < d.srv_count; k++) w.i(c.views[d.srv_begin + k].cont);
				w.end_arr();
			}
			any = false;
			for (uint32_t k = 0; k < d.out_count; k++) any = any || c.views[d.out_begin + k].cont >= 0;
			if (any) {
				w.key("cout"); w.begin_arr();
				for (uint32_t k = 0; k < d.out_count; k++) w.i(c.views[d.out_begin + k].cont);
				w.end_arr();
			}
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
