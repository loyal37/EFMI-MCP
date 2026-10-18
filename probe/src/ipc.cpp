// Named pipe server: \\.\pipe\efmi-probe-<pid>
//
// Framing in both directions: [u32 header_len][u32 payload_len][header JSON][payload]
// One client at a time. All D3D work is done on the game's render thread; this
// thread only queues requests and waits.
#include "common.h"
#include "capture.h"
#include "registry.h"
#include "hooks.h"
#include "hash.h"
#include "json.h"
#include "reload.h"
#include "slotcache.h"
#include <sddl.h>
#include <string.h>

extern const char* g_attach_how;
extern bool g_attach_wrapped;
extern wchar_t g_runtime_path[MAX_PATH];

static bool read_exact(HANDLE h, void* buf, DWORD n)
{
	BYTE* p = (BYTE*)buf;
	while (n) {
		DWORD got = 0;
		if (!ReadFile(h, p, n, &got, nullptr) || !got)
			return false;
		p += got;
		n -= got;
	}
	return true;
}

static bool write_all(HANDLE h, const void* buf, size_t n)
{
	const BYTE* p = (const BYTE*)buf;
	while (n) {
		DWORD chunk = n > (1u << 20) ? (1u << 20) : (DWORD)n;
		DWORD put = 0;
		if (!WriteFile(h, p, chunk, &put, nullptr) || !put)
			return false;
		p += put;
		n -= put;
	}
	return true;
}

static bool send_msg(HANDLE pipe, const std::string& header, const void* payload, size_t payload_len)
{
	uint32_t lens[2] = { (uint32_t)header.size(), (uint32_t)payload_len };
	if (!write_all(pipe, lens, sizeof(lens))) return false;
	if (!write_all(pipe, header.data(), header.size())) return false;
	if (payload_len && !write_all(pipe, payload, payload_len)) return false;
	return true;
}

static bool send_error(HANDLE pipe, double id, const std::string& msg)
{
	JsonW w;
	w.begin_obj();
	w.kd("id", id);
	w.kb("ok", false);
	w.ks("error", msg);
	w.end_obj();
	return send_msg(pipe, w.s, nullptr, 0);
}

static void status_json(JsonW& w)
{
	wchar_t exe[MAX_PATH] = {};
	GetModuleFileNameW(nullptr, exe, MAX_PATH);
	int rw, rh;
	hash_get_resolution(&rw, &rh);
	RegStats rs;
	reg_stats(&rs);

	w.ks("probe", PROBE_VERSION);
	w.ku("protocol", PROBE_PROTOCOL);
	// Optional abilities, so the server need not compare version strings.
	w.key("features"); w.begin_arr(); w.str("reload_key"); w.str("create_frames"); w.str("mods_key");
	w.str("region_hash_per_object"); w.str("pipeline_state"); w.str("rt_snapshots"); w.str("contents");
	w.str("slot_cache"); w.end_arr();
	bool elevated = false;
	uint32_t integrity = 0;
	reload_process_info(&elevated, &integrity);
	w.key("process"); w.begin_obj();
	w.kb("elevated", elevated);
	w.ku("integrity_rid", integrity);
	w.end_obj();
	w.ku("pid", GetCurrentProcessId());
	w.ks("exe", narrow(exe));
	w.kb("attached", g_dev && g_ctx);
	w.ks("attached_via", g_attach_how ? g_attach_how : "");
	// True when the object handed to us was XXMI's wrapper rather than the runtime's.
	w.kb("saw_wrapper", g_attach_wrapped);
	w.ks("runtime", narrow(g_runtime_path));
	w.kb("swap_chain", g_swap != nullptr);
	w.key("capture_target"); w.begin_obj();
	w.key("device"); w.ptr(g_dev ? g_dev->obj : nullptr);
	w.key("context"); w.ptr(g_ctx ? g_ctx->obj : nullptr);
	w.key("swap_chain"); w.ptr(g_swap ? g_swap->obj : nullptr);
	w.end_obj();
	w.kb("context1", g_ctx_has_ctx1);
	w.ku("feature_level", (uint32_t)g_feature_level);
	w.ku("frame", (uint32_t)g_frame_no);
	w.key("resolution"); w.begin_arr(); w.i(rw); w.i(rh); w.end_arr();

	w.key("xxmi"); w.begin_obj();
	w.kb("present", g_xxmi.base != 0);
	w.ks("path", narrow(g_xxmi.path));
	w.end_obj();

	w.key("counters"); w.begin_obj();
	w.ku("draw_calls", (uint32_t)g_hstats.draws);
	w.ku("tracked_draw_calls", (uint32_t)g_hstats.tracked_draws);
	w.ku("creations", (uint32_t)g_hstats.creates);
	w.ku("presents", (uint32_t)g_hstats.presents);
	w.ku("maps", (uint32_t)g_hstats.maps);
	w.ku("command_lists", (uint32_t)g_hstats.exec_cmdlists);
	w.ku("vtable_reapplied", (uint32_t)g_hstats.revptr);
	w.ku("xxmi_resource_creates", (uint32_t)g_hstats.xxmi_creates);
	w.ku("xxmi_shader_creates", (uint32_t)g_hstats.xxmi_shader_creates);
	w.end_obj();

	w.key("registry"); w.begin_obj();
	w.ku("resources", rs.resources);
	w.ku("shaders", rs.shaders);
	w.ku("layouts", rs.layouts);
	w.ku("bytecode_count", rs.bytecode_count);
	w.ku("bytecode_bytes", rs.bytecode_bytes);
	w.ku("created_total", rs.created_total);
	w.end_obj();

	w.key("xxmi_settings"); w.begin_obj();
	w.ki("texture_hash", g_cfg.texture_hash);
	w.kb("track_region_hashes", g_cfg.track_region_hashes);
	w.ki("shader_hash", g_cfg.shader_hash);
	w.kb("resolution_from_swap_chain", g_cfg.res_from_swap_chain);
	w.end_obj();

	std::string extra;
	cap_status_json(&extra);
	w.key("capture"); w.raw("{" + extra + "}");
	std::string slots;
	slot_status_object(&slots);
	w.key("slot_cache"); w.raw(slots);
}

// Which draws a request is about; see DrawMatch.
static void parse_matches(const JVal* list, std::vector<DrawMatch>* out)
{
	if (!list || list->type != JVal::Arr)
		return;
	for (const JVal& m : list->arr) {
		if (m.type != JVal::Obj || out->size() >= 256)
			continue;
		DrawMatch dm;
		dm.draw = (int32_t)m.num_or("draw", -1);
		dm.first = (int32_t)m.num_or("first", -1);
		dm.last = (int32_t)m.num_or("last", -1);
		dm.ib = (void*)(uintptr_t)_strtoui64(m.str_or("ib", "0").c_str(), nullptr, 16);
		dm.ib_serial = (uint64_t)m.num_or("sn", 0);
		dm.index_count = (uint32_t)m.num_or("ic", 0);
		dm.first_index = (int64_t)m.num_or("fi", -1);
		dm.vs = _strtoui64(m.str_or("vs", "0").c_str(), nullptr, 16);
		dm.ps = _strtoui64(m.str_or("ps", "0").c_str(), nullptr, 16);
		dm.shader = _strtoui64(m.str_or("sh", "0").c_str(), nullptr, 16);
		std::string origin = m.str_or("o", "");
		dm.origin = origin == "game" ? ORIGIN_GAME : origin == "xxmi" ? ORIGIN_XXMI : -1;
		if (dm.any())
			out->push_back(dm);
	}
}

static uint32_t kinds_of(const JVal* list)
{
	uint32_t kinds = 0;
	if (!list || list->type != JVal::Arr)
		return 0;
	for (const JVal& k : list->arr) {
		if (k.type != JVal::Str) continue;
		if (k.str == "ib") kinds |= CK_IB;
		else if (k.str == "vb") kinds |= CK_VB;
		else if (k.str == "cb") kinds |= CK_CB;
		else if (k.str == "buf") kinds |= CK_BUF;
		else if (k.str == "tex") kinds |= CK_TEX;
		else if (k.str == "uav") kinds |= CK_UAV;
		else if (k.str == "rt") kinds |= CK_RT;
		else if (k.str == "depth") kinds |= CK_DEPTH;
	}
	return kinds;
}

static bool handle(HANDLE pipe, const JVal& rq)
{
	std::string cmd = rq.str_or("cmd", "");
	double id = rq.num_or("id", 0);
	uint32_t timeout = (uint32_t)rq.num_or("timeout_ms", 5000);

	if (cmd == "hello" || cmd == "status" || cmd == "ping") {
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		status_json(w);
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	if (cmd == "capture") {
		CaptureRequest c;
		std::string detail = rq.str_or("detail", "full");
		c.detail = (detail == "minimal") ? -1 : (detail == "light") ? 0 : 1;
		c.hash_ib = rq.bool_or("hash_ib", true);
		c.hash_vb = rq.bool_or("hash_vb", true);
		c.cb_data = rq.bool_or("cb_data", false);
		c.hold = rq.bool_or("hold", true);
		c.max_draws = (uint32_t)rq.num_or("max_draws", 0);
		c.timeout_ms = timeout;
		if (const JVal* rt = rq.get("rt")) {
			// Copies of the render target around the draws that match; see DrawMatch.
			if (rt->type != JVal::Obj)
				return send_error(pipe, id, "rt must be an object");
			double n = rt->num_or("max", 8);
			c.rt_max = n < 1 ? 1 : n > 64 ? 64 : (uint32_t)n;
			n = rt->num_or("budget_mb", 512);
			c.rt_budget_mb = n < 16 ? 16 : n > 4096 ? 4096 : (uint32_t)n;
			c.rt_before = rt->bool_or("before", true);
			c.rt_all = rt->bool_or("all", false);
			c.rt_depth = rt->bool_or("depth", false);
			const JVal* box = rt->get("box");
			if (box && box->type == JVal::Arr && box->arr.size() == 4) {
				for (int i = 0; i < 4; i++)
					c.rt_box[i] = box->arr[i].type == JVal::Num && box->arr[i].num > 0 ? (uint32_t)box->arr[i].num : 0;
				c.rt_has_box = c.rt_box[2] > c.rt_box[0] && c.rt_box[3] > c.rt_box[1];
			}
			parse_matches(rt->get("match"), &c.rt_match);
			if (c.rt_match.empty())
				return send_error(pipe, id, "rt.match selects no draw: give draw, ib, ic, fi, vs, ps, sh or o");
			c.detail = 1;       // the copies belong to a full record
		}
		if (const JVal* ct = rq.get("contents")) {
			// What calls saw, kept as it was: "inputs" for every call (ib, vb, cb, buf, tex), "outputs"
			// (rt, depth, uav) for the calls that "match" selects or for "all" of them.
			if (ct->type != JVal::Obj)
				return send_error(pipe, id, "contents must be an object");
			c.ct_kinds = kinds_of(ct->get("inputs")) & (CK_IB | CK_VB | CK_CB | CK_BUF | CK_TEX);
			c.ct_out_kinds = kinds_of(ct->get("outputs")) & (CK_UAV | CK_RT | CK_DEPTH);
			parse_matches(ct->get("match"), &c.ct_match);
			c.ct_out_all = ct->bool_or("all", false);
			c.ct_events = ct->bool_or("events", false);
			c.ct_strict = ct->bool_or("strict", false);
			double mb = ct->num_or("budget_mb", 1024);
			mb = mb < 0.001 ? 0.001 : mb > 16384 ? 16384 : mb;
			c.ct_budget = (uint64_t)(mb * 1048576.0);
			if (c.ct_out_kinds && !c.ct_out_all && c.ct_match.empty())
				return send_error(pipe, id, "contents.outputs needs contents.match (draw, first/last, ib, ic, fi, vs, ps, sh, o) or all");
			if (!c.contents() && !c.ct_events)
				return send_error(pipe, id, "contents asks for nothing: give inputs, outputs or events");
			c.detail = 1;
			c.hold = true;      // what is read in place has to stay alive
		}
		std::string err;
		std::shared_ptr<Capture> cap = cap_request(c, &err);
		if (!cap)
			return send_error(pipe, id, err);
		std::string json;
		std::vector<uint8_t> blob;
		cap_serialize(*cap, &json, &blob);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.ku("frame", cap_frame_of(*cap));
		w.ku("json_len", json.size());
		w.ku("bin_len", blob.size());
		w.end_obj();
		std::vector<uint8_t> payload;
		payload.reserve(json.size() + blob.size());
		payload.insert(payload.end(), json.begin(), json.end());
		payload.insert(payload.end(), blob.begin(), blob.end());
		return send_msg(pipe, w.s, payload.data(), payload.size());
	}

	if (cmd == "read_buffer" || cmd == "read_texture" || cmd == "read_rt" || cmd == "read_content") {
		ReadResult r;
		uint32_t frame = (uint32_t)rq.num_or("capture", 0);
		uint32_t res = (uint32_t)rq.num_or("res", 0);
		if (cmd == "read_buffer")
			cap_read_buffer(frame, res, (uint64_t)rq.num_or("offset", 0), (uint64_t)rq.num_or("size", 0), timeout, &r);
		else if (cmd == "read_rt")
			cap_read_rt(frame, (uint32_t)rq.num_or("snap", 0), timeout, &r);
		else if (cmd == "read_content")
			cap_read_content(frame, (uint32_t)rq.num_or("snap", 0), (uint64_t)rq.num_or("offset", 0),
			                 (uint64_t)rq.num_or("size", 0), timeout, &r);
		else
			cap_read_texture(frame, res, (uint32_t)rq.num_or("mip", 0), (uint32_t)rq.num_or("slice", 0), timeout, &r);
		if (!r.ok)
			return send_error(pipe, id, r.err.empty() ? "read failed" : r.err);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.key("meta"); w.raw(r.meta.empty() ? "{}" : r.meta);
		w.end_obj();
		return send_msg(pipe, w.s, r.data.data(), r.data.size());
	}

	if (cmd == "shader") {
		std::string hs = rq.str_or("hash", "");
		uint64_t hash = _strtoui64(hs.c_str(), nullptr, 16);
		std::vector<uint8_t> code;
		std::string type;
		if (!reg_get_bytecode(hash, &code, &type) || code.empty())
			return send_error(pipe, id, "no bytecode kept for that shader hash (created before the probe attached, or keep_bytecode is off)");
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.ks("type", type);
		w.kh64("hash", hash);
		w.ku("size", code.size());
		w.end_obj();
		return send_msg(pipe, w.s, code.data(), code.size());
	}

	if (cmd == "live") {
		LiveConfig lc;
		lc.enable = rq.bool_or("enable", true);
		lc.hash_ib = rq.bool_or("hash_ib", true);
		lc.hash_vb = rq.bool_or("hash_vb", false);
		lc.hash_volatile = rq.bool_or("hash_volatile", false);
		lc.keep_frames = (uint32_t)rq.num_or("keep_frames", 240);
		lc.every = (uint32_t)rq.num_or("every", 1);
		cap_live_configure(lc);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.kb("live", lc.enable);
		w.ku("frame", (uint32_t)g_frame_no);
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	if (cmd == "slot_cache") {
		// Settings of the slot cache, changed for as long as the game runs; `flush` waits until
		// everything recorded so far is written.
		SlotCacheConfig sc;
		slot_get_config(&sc);
		sc.enable = rq.bool_or("enable", sc.enable);
		sc.every = (uint32_t)rq.num_or("every", sc.every);
		sc.skinned_only = rq.bool_or("skinned_only", sc.skinned_only);
		sc.bones_only = rq.bool_or("bones_only", sc.bones_only);
		sc.static_only = rq.bool_or("static_only", sc.static_only);
		sc.max_new = (uint32_t)rq.num_or("max_new", sc.max_new);
		sc.subdir = rq.str_or("subdir", sc.subdir.c_str());
		if (!slot_set_config(sc))
			return send_error(pipe, id, "subdir must be a plain folder name (letters, digits, '.', '-', '_')");
		bool flushed = true;
		if (rq.bool_or("flush", false))
			flushed = slot_flush((uint32_t)rq.num_or("timeout_ms", 5000));
		std::string status;
		slot_status_object(&status);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.kb("flushed", flushed);
		w.key("slot_cache"); w.raw(status);
		w.ku("frame", (uint32_t)g_frame_no);
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	if (cmd == "live_get") {
		std::string frames;
		cap_live_get((uint32_t)rq.num_or("since", 0), (uint32_t)rq.num_or("max_frames", 0), &frames);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.ku("frame", (uint32_t)g_frame_no);
		w.end_obj();
		return send_msg(pipe, w.s, frames.data(), frames.size());
	}

	if (cmd == "release_rt" || cmd == "release_content") {
		if (cmd == "release_rt") cap_release_rt((uint32_t)rq.num_or("capture", 0), timeout);
		else cap_release_content((uint32_t)rq.num_or("capture", 0), timeout);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	if (cmd == "release") {
		cap_release(timeout);
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	if (cmd == "reload_key" || cmd == "mods_key") {
		// Two fixed functions with a fixed key each, to this process's own window
		// and nothing else: F10 reloads XXMI's configuration, F11 switches all
		// mods off or on (EFMI's own key).
		KeyPressRequest q;
		q.key = cmd == "mods_key" ? GameKey::ToggleMods : GameKey::Reload;
		q.focus = rq.bool_or("focus", true);
		q.restore_focus = rq.bool_or("restore_focus", true);
		double hold = rq.num_or("max_hold_ms", 3000);
		q.max_hold_ms = hold < 200 ? 200 : hold > 10000 ? 10000 : (uint32_t)hold;
		JsonW w;
		w.begin_obj();
		w.kd("id", id);
		w.kb("ok", true);
		w.key("result"); w.begin_obj();
		std::string message;
		const char* code = key_press_run(q, w, &message);
		w.ks("code", code);
		w.ks("message", message);
		w.end_obj();
		w.end_obj();
		return send_msg(pipe, w.s, nullptr, 0);
	}

	return send_error(pipe, id, "unknown command: " + cmd);
}

// The game usually runs elevated while the MCP server does not: grant the
// account that owns this process explicit access.
static bool make_security(SECURITY_ATTRIBUTES* sa)
{
	HANDLE tok = nullptr;
	if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok))
		return false;
	BYTE buf[512];
	DWORD len = 0;
	bool ok = false;
	if (GetTokenInformation(tok, TokenUser, buf, sizeof(buf), &len)) {
		LPWSTR sid = nullptr;
		if (ConvertSidToStringSidW(((TOKEN_USER*)buf)->User.Sid, &sid)) {
			wchar_t sddl[512];
			swprintf_s(sddl, L"D:(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)", sid);
			PSECURITY_DESCRIPTOR sd = nullptr;
			if (ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, &sd, nullptr)) {
				sa->nLength = sizeof(*sa);
				sa->lpSecurityDescriptor = sd;
				sa->bInheritHandle = FALSE;
				ok = true;
			}
			LocalFree(sid);
		}
	}
	CloseHandle(tok);
	return ok;
}

static DWORD WINAPI pipe_thread(LPVOID)
{
	wchar_t name[128];
	swprintf_s(name, L"\\\\.\\pipe\\efmi-probe-%lu", GetCurrentProcessId());
	SECURITY_ATTRIBUTES sa = {};
	bool have_sa = make_security(&sa);
	plog("ipc: listening on %s", narrow(name).c_str());

	for (;;) {
		HANDLE pipe = CreateNamedPipeW(name, PIPE_ACCESS_DUPLEX,
		                               PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
		                               1, 1 << 20, 1 << 20, 0, have_sa ? &sa : nullptr);
		if (pipe == INVALID_HANDLE_VALUE) {
			plog("ipc: CreateNamedPipe failed %lu", GetLastError());
			Sleep(2000);
			continue;
		}
		if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
			CloseHandle(pipe);
			continue;
		}
		plog("ipc: client connected");
		cap_client_connected();

		for (;;) {
			uint32_t lens[2];
			if (!read_exact(pipe, lens, sizeof(lens)))
				break;
			if (lens[0] > (1u << 20) || lens[1] > (1u << 20))
				break;
			std::string header(lens[0], '\0');
			if (lens[0] && !read_exact(pipe, &header[0], lens[0]))
				break;
			std::string skip(lens[1], '\0');
			if (lens[1] && !read_exact(pipe, &skip[0], lens[1]))
				break;
			JVal rq;
			if (!JsonR::parse(header.data(), header.size(), &rq) || rq.type != JVal::Obj) {
				if (!send_error(pipe, 0, "malformed request"))
					break;
				continue;
			}
			if (!handle(pipe, rq))
				break;
		}

		cap_client_disconnected();
		plog("ipc: client disconnected");
		FlushFileBuffers(pipe);
		DisconnectNamedPipe(pipe);
		CloseHandle(pipe);
	}
}

void ipc_start()
{
	HANDLE t = CreateThread(nullptr, 0, pipe_thread, nullptr, 0, nullptr);
	if (t)
		CloseHandle(t);
}
