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
	void*    ib = nullptr;       // index buffer object
	uint64_t ib_serial = 0;      // its creation serial; 0 accepts any object at that address
	uint32_t index_count = 0;    // 0: any
	int64_t  first_index = -1;   // -1: any
	uint64_t vs = 0, ps = 0;     // shader hashes; 0: any
	uint64_t shader = 0;         // the vertex or the pixel shader
	int      origin = -1;        // Origin; -1: any
	bool     any() const { return draw >= 0 || ib || index_count || first_index >= 0 || vs || ps || shader || origin >= 0; }
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
};

struct Capture;

// ---- render thread (called from hooks)
extern volatile LONG g_recording;   // a frame is being recorded
extern volatile LONG g_tracking;    // buffer write tracking is on (a client is connected)
extern volatile LONG g_rt_after;    // the draw being issued wants its render target copied afterwards

void cap_init();
void cap_on_draw(uint8_t type, const DrawArgs& a, const uint32_t* thread_groups,
                 void* indirect_buffer, uint32_t indirect_offset, const CallerInfo& caller);
void cap_after_draw();                              // the draw that set g_rt_after has been issued
void cap_on_frame_boundary();
// A CPU write or copy into [off, off+size) of a resource (size 0xffffffff: unknown extent).
void cap_buffer_written(void* resource, uint32_t off = 0, uint32_t size = 0xffffffffu);
void cap_gpu_writes_cs();                           // a Dispatch may write the bound CS UAVs
void cap_gpu_writes_om();                           // a draw may write OM UAVs / SO targets
void cap_set_cs_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs);
void cap_set_om_uavs(UINT start, UINT n, ID3D11UnorderedAccessView* const* uavs);
void cap_set_so_targets(UINT n, ID3D11Buffer* const* targets);
void cap_state_cleared();
void cap_invalidate_all();                          // unknown writes happened (command list replay)

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
void cap_status_json(std::string* out);             // appended as JSON members
