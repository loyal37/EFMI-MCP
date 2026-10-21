// Frame capture: what the real device context is asked to draw, with the
// identity (XXMI-compatible hashes) of everything bound at that moment.
#pragma once
#include "common.h"
#include "hash.h"
#include "hooks.h"
#include <memory>

// Same numbering as XXMI's DrawCall enum (DrawCallInfo.h) / the `draw_type` ini operand.
enum DrawType : uint8_t {
	DT_DRAW = 1, DT_DRAW_INDEXED = 2, DT_DRAW_INSTANCED = 3, DT_DRAW_INDEXED_INSTANCED = 4,
	DT_DRAW_INSTANCED_INDIRECT = 5, DT_DRAW_INDEXED_INSTANCED_INDIRECT = 6, DT_DRAW_AUTO = 7,
	DT_DISPATCH = 8, DT_DISPATCH_INDIRECT = 9,
};

// Selects draws by what is known while they are issued. Every field that is set has to match.
struct DrawMatch {
	int32_t  draw = -1;          // number of the draw in its frame
	int32_t  first = -1, last = -1;  // or a range of numbers, both ends included
	void*    ib = nullptr;       // index buffer object
	uint64_t ib_serial = 0;      // its creation serial; 0 accepts any object at that address
	uint32_t index_count = 0;    // 0: any
	int64_t  first_index = -1;   // -1: any
	uint64_t vs = 0, ps = 0;     // shader hashes; 0: any
	uint64_t shader = 0;         // the vertex or the pixel shader
	int      origin = -1;        // Origin; -1: any
	bool     any() const { return draw >= 0 || first >= 0 || last >= 0 || ib || index_count || first_index >= 0 || vs || ps || shader || origin >= 0; }
};

struct CaptureRequest {
	int      detail = 1;         // -1 = minimal (index buffer + VS/PS), 0 = light (IA + shaders), 1 = full
	bool     hash_ib = true;
	bool     hash_vb = true;
	bool     cb_data = false;    // copy constant buffer contents as bound at each draw
	bool     hold = true;        // keep references so resources can be read afterwards
	uint32_t max_draws = 0;      // 0 = settings default
	uint32_t timeout_ms = 5000;

	// Copies of render targets around chosen draws (full captures only).
	std::vector<DrawMatch> rt_match;
	uint32_t rt_max = 8;         // at most this many draws
	bool     rt_before = true;   // the target as it was just before the draw, too
	bool     rt_all = false;     // every bound colour target instead of the first
	bool     rt_depth = false;   // the depth-stencil target as well
	bool     rt_has_box = false;
	uint32_t rt_box[4] = {};     // left, top, right, bottom in target pixels
	uint32_t rt_budget_mb = 512; // of copied pixels per capture

	// Contents as they were when a call was issued (full captures only). Nothing
	// is copied for that while the frame is recorded: see "contents" in capture.cpp.
	uint32_t ct_kinds = 0;       // CK_*: what every call's inputs are noted for
	uint32_t ct_out_kinds = 0;   // CK_RT / CK_DEPTH / CK_UAV: what a selected call leaves behind
	std::vector<DrawMatch> ct_match;   // the calls whose outputs are wanted ...
	bool     ct_out_all = false; // ... or every call
	uint64_t ct_budget = 1024ull << 20;  // bytes of copies the capture may come to hold
	bool     ct_events = false;  // list the writes, copies and clears between the calls
	// A write that promises to leave alone what is in use (D3D11_MAP_WRITE_NO_OVERWRITE) is taken
	// at its word unless this is set: then it starts a new version like any other write, and a
	// buffer is also exact where no call was reading it. Costs a copy of the buffer per such write.
	bool     ct_strict = false;
	bool     contents() const { return ct_kinds || ct_out_kinds; }
};

// What can be kept of a call (CaptureRequest::ct_kinds, ct_out_kinds).
enum : uint32_t {
	CK_IB = 1, CK_VB = 2, CK_CB = 4, CK_BUF = 8, CK_TEX = 16,   // inputs: buffers and textures bound for reading
	CK_UAV = 32, CK_RT = 64, CK_DEPTH = 128,                    // outputs, as the call left them
};

// The writes a frame lists when asked to (CaptureRequest::ct_events).
enum : uint8_t {
	EV_MAP = 1, EV_UNMAP, EV_UPDATE, EV_COPY, EV_COPY_REGION, EV_COPY_COUNT, EV_DISCARD,
	EV_CLEAR_RTV, EV_CLEAR_DSV, EV_CLEAR_UAV_UINT, EV_CLEAR_UAV_FLOAT, EV_GENERATE_MIPS, EV_RESOLVE,
	EV_COMMAND_LIST, EV_CLEAR_STATE, EV_CLEAR_VIEW, EV_DISCARD_VIEW,
};

struct Capture;

// ---- render thread (called from hooks)
extern volatile LONG g_recording;   // a frame is being recorded
extern volatile LONG g_tracking;    // buffer write tracking is on (a client is connected)
extern volatile LONG g_after;       // the call being issued has something to be done once it has run
extern volatile LONG g_barrier;     // a capture keeps contents: every write has to be announced first
extern volatile LONG g_events;      // the frame being recorded lists its writes
extern volatile LONG g_caller_late; // the frame being recorded works out the caller of a draw itself,
                                    // only for the draws it keeps (caller_from_probe)

void cap_init();
void cap_on_draw(uint8_t type, const DrawArgs& a, const uint32_t* thread_groups,
                 void* indirect_buffer, uint32_t indirect_offset, const CallerInfo& caller);
void cap_after_draw();                              // the call that set g_after has been issued
void cap_on_frame_boundary();
// A CPU write or copy into [off, off+size) of a resource (size 0xffffffff: unknown extent).
void cap_buffer_written(void* resource, uint32_t off = 0, uint32_t size = 0xffffffffu);
void cap_gpu_writes_cs();                           // a Dispatch may write the bound CS UAVs
void cap_gpu_writes_om();                           // a draw may write OM UAVs / SO targets
void cap_set_cs_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs);
void cap_set_om_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs);
void cap_set_so_targets(UINT n, ID3D11Buffer* const* targets);
void cap_state_cleared();
void cap_state_swapped();                           // another device context state was swapped in
void cap_view_written(ID3D11View* view);            // a write through a view of a buffer
void cap_invalidate_all();                          // unknown writes happened (command list replay)

// ---- contents: called before the runtime is asked to change a resource (g_barrier is set)
// A write into subresource `sub` (0xffffffff: any of them); for buffers into [off, off+size)
// (size 0xffffffff: to the end). keeps_rest: the caller promises not to touch what is in use
// (D3D11_MAP_WRITE_NO_OVERWRITE).
void cap_before_write(ID3D11Resource* res, uint32_t sub, uint32_t off, uint32_t size, bool keeps_rest);
// A write through a view: a clear (kind 1 render target, 2 depth-stencil, 3 unordered access)
// or GenerateMips (kind 0, shader resource).
void cap_before_view_write(ID3D11View* view, int kind);
void cap_before_call_writes(bool dispatch);         // what the call about to be issued may write
void cap_before_unknown_writes();                   // a command list: nothing is known about its writes
void cap_outputs_changed(bool compute);             // output merger or compute UAV bindings were set
void cap_mapped(ID3D11Resource* res, bool mapped);
// One line of the frame's list of writes (g_events is set). a: up to four numbers that describe it.
void cap_event(uint8_t type, ID3D11Resource* dst, ID3D11Resource* src, uint32_t a0 = 0, uint32_t a1 = 0,
               uint32_t a2 = 0, uint32_t a3 = 0, const float* f = nullptr);
void cap_event_view(uint8_t type, ID3D11View* view, uint32_t a0 = 0, uint32_t a1 = 0, const float* f = nullptr);

// ---- IPC thread
void cap_client_connected();
void cap_client_disconnected();
// Re-sync GPU writer bindings at the first Present of a selected swap chain.
void cap_target_selected();

std::shared_ptr<Capture> cap_request(const CaptureRequest& rq, std::string* err);
void cap_serialize(const Capture& c, std::string* json, std::vector<uint8_t>* blob);
uint32_t cap_frame_of(const Capture& c);

struct ReadResult {
	bool ok = false;
	std::string err;
	std::string meta;            // JSON object body (without braces)
	std::vector<uint8_t> data;
};
void cap_read_buffer(uint32_t capture_frame, uint32_t res_id, uint64_t offset, uint64_t size,
                     uint32_t timeout_ms, ReadResult* out);
void cap_read_texture(uint32_t capture_frame, uint32_t res_id, uint32_t mip, uint32_t slice,
                      uint32_t timeout_ms, ReadResult* out);
// A render target copy taken around a draw (index into the capture's "rts").
void cap_read_rt(uint32_t capture_frame, uint32_t snap, uint32_t timeout_ms, ReadResult* out);
// Frees those copies once they have been read; the rest of the capture stays readable.
void cap_release_rt(uint32_t capture_frame, uint32_t timeout_ms);
// What a slot held when its call was issued (index into the capture's "cont"). For a buffer
// offset/size select a part; size 0 reads to the end.
void cap_read_content(uint32_t capture_frame, uint32_t snap, uint64_t offset, uint64_t size,
                      uint32_t timeout_ms, ReadResult* out);
// Lets go of the contents a capture keeps: from then on the game's writes are no longer watched.
void cap_release_content(uint32_t capture_frame, uint32_t timeout_ms);
void cap_release(uint32_t timeout_ms);

// ---- continuous mode: every frame is recorded at minimal detail and summarised
struct LiveConfig {
	bool     enable = false;
	bool     hash_ib = true;
	bool     hash_vb = false;
	bool     hash_volatile = false;  // also hash buffers that are rewritten every frame
	uint32_t keep_frames = 240;      // ring size
	uint32_t every = 1;              // record every n-th frame
};
void cap_live_configure(const LiveConfig& cfg);
// Appends a JSON array of frame summaries newer than `since_frame`.
void cap_live_get(uint32_t since_frame, uint32_t max_frames, std::string* json);

// A stretch of the continuous stream added up per mesh/shader line: in how many frames each line
// was drawn, first and last frame, the most draws and instances, and in how many frames of an
// earlier baseline stretch it was drawn too. Frames are chosen by number or, when `seconds` is
// set, as the last `seconds` of the stream (and the `base_seconds` before them).
struct LiveQuery {
	uint32_t first = 0, last = 0;            // 0: from the oldest / to the newest frame kept
	uint32_t base_first = 0, base_last = 0;  // both 0: no baseline
	double   seconds = 0, base_seconds = 0;
	bool     timeline = true;                // one entry per frame of the stretch as well
};
void cap_live_summary(const LiveQuery& q, std::string* json);

// A capture that starts on its own when something appears: a mesh or shader the client named, or
// a vertex/pixel shader pair not drawn while a baseline was watched. The frame after the one it was
// seen in (plus delay_frames) is recorded with `rq`, contents included.
struct TriggerSpec {
	std::vector<uint32_t> ib;        // index buffer region hashes
	std::vector<uint64_t> shader;    // shader hashes of any stage
	bool     new_pairs = false;      // a vertex/pixel shader pair not drawn during the baseline
	uint32_t baseline_frames = 60;
	int      origin = -1;            // Origin of the draws that count; -1: any
	uint32_t min_count = 0;          // draws with fewer indices (vertices when not indexed) do not count
	uint32_t delay_frames = 0;
	uint32_t expire_ms = 600000;     // 0: never
	CaptureRequest rq;
};
bool cap_trigger_arm(const TriggerSpec& spec, std::string* err);
void cap_trigger_cancel();
// Waits up to timeout_ms while the trigger is waiting or recording. `state` gets a JSON object
// describing it; the capture is returned once, when it has been recorded (the trigger is off then).
std::shared_ptr<Capture> cap_trigger_wait(uint32_t timeout_ms, std::string* state);
void cap_status_json(std::string* out);             // appended as JSON members
