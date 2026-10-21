// Recording: the folder of a recording and the thread that writes into it (see record.h).
//
// A recording's folder:
//   recording.json   what was recorded, by what, when; rewritten when it stops
//   lines.jsonl      one line per mesh/shader line the first time it is drawn: {"id", "line": [...]}
//   runs.jsonl       [id, first frame, last frame, frames, most draws, most instances, all draws]: a
//                    stretch of recorded frames in which the line was drawn in every one
//   open-runs.json   the runs not finished yet, rewritten every few seconds; gone once stopped
//   frames.bin       per recorded frame 5 x u32: frame, ms since the start, draws, game draws, lines
//   keyframes.jsonl  per frame recorded in full: frame, time, why, its files
//   keyframes\       <frame>.json and <frame>.bin, what a "capture" answers with
#include "record.h"
#include "capture.h"
#include "json.h"
#include <deque>
#include <wctype.h>

static RecordConfig g_rc;

static CRITICAL_SECTION g_cs;
static CONDITION_VARIABLE g_cv_work, g_cv_idle;
static HANDLE g_thread = nullptr;
static bool g_inited = false;

enum { T_OPEN = 1, T_LINES, T_RUNS, T_FRAMES, T_OPEN_RUNS, T_KEYFRAME, T_CLOSE };
struct Task {
	int type = 0;
	uint32_t id = 0;
	std::string text, reason, what;
	std::vector<uint8_t> bin;
	std::shared_ptr<Capture> cap;
	uint32_t frame = 0, t_ms = 0;
};
static std::deque<Task> g_q;                    // guarded by g_cs
static uint64_t g_submitted = 0, g_done = 0;    // guarded by g_cs
static uint32_t g_next_id = 0;                  // render thread

// ---- status (guarded by g_cs)
static uint32_t g_st_id = 0;
static std::string g_st_folder, g_st_error;
static volatile LONG64 g_st_bytes = 0;

// ---- writer thread only
struct Rec {
	uint32_t id = 0;
	std::wstring dir;
	HANDLE lines = INVALID_HANDLE_VALUE, runs = INVALID_HANDLE_VALUE, frames = INVALID_HANDLE_VALUE,
	       keyframes = INVALID_HANDLE_VALUE;
	std::string head;                           // the members of recording.json written at the start
	bool ok = false;
};
static Rec g_w;

static std::wstring widen(const std::string& s)
{
	if (s.empty()) return std::wstring();
	int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
	std::wstring w((size_t)n, L'\0');
	MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], n);
	return w;
}

static bool is_dir(const std::wstring& p)
{
	DWORD a = GetFileAttributesW(p.c_str());
	return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}

static const char* key_name(uint32_t vk)
{
	static char buf[16];
	if (vk >= VK_F1 && vk <= VK_F24) { snprintf(buf, sizeof(buf), "F%u", vk - VK_F1 + 1); return buf; }
	if ((vk >= '0' && vk <= '9') || (vk >= 'A' && vk <= 'Z')) { buf[0] = (char)vk; buf[1] = 0; return buf; }
	snprintf(buf, sizeof(buf), "vk%u", vk);
	return buf;
}

std::string rec_hotkey_name()
{
	std::string s;
	if (g_rc.ctrl) s += "ctrl+";
	if (g_rc.shift) s += "shift+";
	if (g_rc.alt) s += "alt+";
	return s + key_name(g_rc.key);
}

const RecordConfig& rec_config() { return g_rc; }

static DWORD WINAPI writer_main(LPVOID);

void rec_init()
{
	InitializeCriticalSection(&g_cs);
	InitializeConditionVariable(&g_cv_work);
	InitializeConditionVariable(&g_cv_idle);
	wchar_t path[MAX_PATH];
	swprintf_s(path, L"%s\\efmi_probe.ini", probe_dir());
	RecordConfig c;
	c.hotkey = GetPrivateProfileIntW(L"Record", L"hotkey", 1, path) != 0;
	wchar_t key[64] = {};
	GetPrivateProfileStringW(L"Record", L"key", L"ctrl F9", key, 64, path);
	// "ctrl shift alt F9": the modifiers that have to be held, then the key (F1-F24, a letter or digit).
	c.ctrl = c.shift = c.alt = false;
	c.key = 0;
	wchar_t* ctx = nullptr;
	for (wchar_t* tok = wcstok_s(key, L" +", &ctx); tok; tok = wcstok_s(nullptr, L" +", &ctx)) {
		if (!_wcsicmp(tok, L"ctrl")) c.ctrl = true;
		else if (!_wcsicmp(tok, L"shift")) c.shift = true;
		else if (!_wcsicmp(tok, L"alt")) c.alt = true;
		else if ((tok[0] == L'F' || tok[0] == L'f') && tok[1] >= L'1' && tok[1] <= L'9') {
			int n = _wtoi(tok + 1);
			if (n >= 1 && n <= 24) c.key = VK_F1 + (uint32_t)(n - 1);
		} else if (!tok[1] && iswalnum(tok[0])) {
			c.key = (uint32_t)towupper(tok[0]);
		}
	}
	if (!c.key) {
		c.key = VK_F9;
		c.ctrl = true;
		c.shift = c.alt = false;
	}
	c.beep = GetPrivateProfileIntW(L"Record", L"beep", 1, path) != 0;
	c.keyframe_every_s = (uint32_t)GetPrivateProfileIntW(L"Record", L"keyframe_every_s", 10, path);
	c.keyframe_gap_ms = (uint32_t)GetPrivateProfileIntW(L"Record", L"keyframe_gap_ms", 2000, path);
	c.keyframe_cb = GetPrivateProfileIntW(L"Record", L"keyframe_cb", 1, path) != 0;
	c.max_keyframes = (uint32_t)GetPrivateProfileIntW(L"Record", L"max_keyframes", 300, path);
	c.disk_mb = (uint32_t)GetPrivateProfileIntW(L"Record", L"disk_mb", 4096, path);
	c.max_minutes = (uint32_t)GetPrivateProfileIntW(L"Record", L"max_minutes", 60, path);
	// Where: [Record] dir, else the project's captures folder next to the probe's (dist\..\captures),
	// else a "recordings" folder next to the DLL.
	wchar_t dir[MAX_PATH] = {};
	GetPrivateProfileStringW(L"Record", L"dir", L"", dir, MAX_PATH, path);
	std::wstring probe = probe_dir();
	if (dir[0]) {
		c.root = (dir[1] == L':' || (dir[0] == L'\\' && dir[1] == L'\\')) ? std::wstring(dir) : probe + L"\\" + dir;
	} else {
		size_t slash = probe.find_last_of(L"\\/");
		std::wstring captures = slash == std::wstring::npos ? std::wstring() : probe.substr(0, slash) + L"\\captures";
		c.root = !captures.empty() && is_dir(captures) ? captures : probe + L"\\recordings";
	}
	g_rc = c;
	g_inited = true;
	plog("record: %s switches it on and off%s; folders in %s; keyframes every %u s and on new shader pairs",
	     c.hotkey ? rec_hotkey_name().c_str() : "no key", c.beep ? " (with a sound)" : "",
	     narrow(c.root.c_str()).c_str(), c.keyframe_every_s);
}

static void push(Task&& t)
{
	EnterCriticalSection(&g_cs);
	if (!g_thread)
		g_thread = CreateThread(nullptr, 0, writer_main, nullptr, 0, nullptr);
	g_q.push_back(std::move(t));
	g_submitted++;
	LeaveCriticalSection(&g_cs);
	WakeConditionVariable(&g_cv_work);
}

uint32_t rec_open(const char* how, uint32_t frame, int res_w, int res_h)
{
	uint32_t id = ++g_next_id;
	JsonW w;
	w.begin_obj();
	w.ks("probe", PROBE_VERSION);
	wchar_t exe[MAX_PATH] = {};
	GetModuleFileNameW(nullptr, exe, MAX_PATH);
	w.ks("game", narrow(exe));
	w.ku("pid", GetCurrentProcessId());
	w.ks("started_by", how);
	w.ku("start_frame", frame);
	FILETIME now;
	GetSystemTimeAsFileTime(&now);
	uint64_t ft = ((uint64_t)now.dwHighDateTime << 32) | now.dwLowDateTime;
	w.ku("started_unix_ms", (ft - 116444736000000000ull) / 10000);
	SYSTEMTIME lt;
	GetLocalTime(&lt);
	char stamp[32];
	snprintf(stamp, sizeof(stamp), "%04u-%02u-%02u %02u:%02u:%02u", lt.wYear, lt.wMonth, lt.wDay, lt.wHour,
	         lt.wMinute, lt.wSecond);
	w.ks("started_local", stamp);
	w.key("resolution"); w.begin_arr(); w.i(res_w); w.i(res_h); w.end_arr();
	w.ks("hotkey", g_rc.hotkey ? rec_hotkey_name() : std::string());
	w.key("settings"); w.begin_obj();
	w.ku("keyframe_every_s", g_rc.keyframe_every_s);
	w.ku("keyframe_gap_ms", g_rc.keyframe_gap_ms);
	w.kb("keyframe_cb", g_rc.keyframe_cb);
	w.ku("max_keyframes", g_rc.max_keyframes);
	w.ku("disk_mb", g_rc.disk_mb);
	w.ku("max_minutes", g_rc.max_minutes);
	w.end_obj();
	w.end_obj();
	Task t;
	t.type = T_OPEN;
	t.id = id;
	t.text = w.s.substr(1, w.s.size() - 2);     // the members, without the braces
	snprintf(stamp, sizeof(stamp), "recording-%04u%02u%02u-%02u%02u%02u", lt.wYear, lt.wMonth, lt.wDay, lt.wHour,
	         lt.wMinute, lt.wSecond);
	t.reason = stamp;
	push(std::move(t));
	return id;
}

static void push_text(int type, uint32_t id, std::string&& s)
{
	if (s.empty())
		return;
	Task t;
	t.type = type;
	t.id = id;
	t.text = std::move(s);
	push(std::move(t));
}

void rec_lines(uint32_t id, std::string&& jsonl) { push_text(T_LINES, id, std::move(jsonl)); }
void rec_runs(uint32_t id, std::string&& jsonl) { push_text(T_RUNS, id, std::move(jsonl)); }
void rec_open_runs(uint32_t id, std::string&& json)
{
	Task t;
	t.type = T_OPEN_RUNS;
	t.id = id;
	t.text = std::move(json);
	push(std::move(t));
}

void rec_frames(uint32_t id, std::vector<uint8_t>&& records)
{
	if (records.empty())
		return;
	Task t;
	t.type = T_FRAMES;
	t.id = id;
	t.bin = std::move(records);
	push(std::move(t));
}

void rec_keyframe(uint32_t id, std::shared_ptr<Capture> c, const char* reason, const std::string& what, uint32_t t_ms)
{
	Task t;
	t.type = T_KEYFRAME;
	t.id = id;
	t.cap = std::move(c);
	t.reason = reason;
	t.what = what;
	t.t_ms = t_ms;
	push(std::move(t));
}

void rec_close(uint32_t id, std::string&& members)
{
	Task t;
	t.type = T_CLOSE;
	t.id = id;
	t.text = std::move(members);
	push(std::move(t));
}

uint64_t rec_bytes(uint32_t id)
{
	EnterCriticalSection(&g_cs);
	uint64_t b = g_st_id == id ? (uint64_t)g_st_bytes : 0;
	LeaveCriticalSection(&g_cs);
	return b;
}

void rec_status_members(std::string* out)
{
	JsonW w;
	w.begin_obj();
	EnterCriticalSection(&g_cs);
	w.ks("folder", g_st_folder);
	w.ks("error", g_st_error);
	w.ku("bytes", (uint64_t)g_st_bytes);
	w.ku("pending", g_submitted - g_done);
	LeaveCriticalSection(&g_cs);
	w.ks("root", narrow(g_rc.root.c_str()));
	w.end_obj();
	*out += w.s.substr(1, w.s.size() - 2);
}

bool rec_flush(uint32_t timeout_ms)
{
	if (!g_inited)
		return true;
	uint64_t until = now_ms() + timeout_ms;
	EnterCriticalSection(&g_cs);
	uint64_t target = g_submitted;
	bool ok = true;
	while (g_done < target) {
		uint64_t now = now_ms();
		if (now >= until) { ok = false; break; }
		SleepConditionVariableCS(&g_cv_idle, &g_cs, (DWORD)(until - now));
	}
	LeaveCriticalSection(&g_cs);
	return ok;
}

// ================================================================ writer thread
static void fail(const std::string& e)
{
	EnterCriticalSection(&g_cs);
	if (g_st_error.empty())
		g_st_error = e;
	LeaveCriticalSection(&g_cs);
	plog("record: %s", e.c_str());
}

static void add_bytes(uint64_t n) { InterlockedExchangeAdd64(&g_st_bytes, (LONG64)n); }

static HANDLE open_append(const std::wstring& path)
{
	return CreateFileW(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
}

static bool write_all(HANDLE f, const void* p, size_t n)
{
	const uint8_t* b = (const uint8_t*)p;
	while (n) {
		DWORD chunk = n > (16u << 20) ? (16u << 20) : (DWORD)n, put = 0;
		if (!WriteFile(f, b, chunk, &put, nullptr) || !put)
			return false;
		b += put;
		n -= put;
	}
	return true;
}

static bool append(HANDLE f, const void* p, size_t n, const char* what)
{
	if (f == INVALID_HANDLE_VALUE)
		return false;
	if (!write_all(f, p, n)) {
		fail(std::string("could not write ") + what + " (" + std::to_string(GetLastError()) + ")");
		return false;
	}
	add_bytes(n);
	return true;
}

// Written beside and moved over the old one, so that a reader never sees half a file.
static bool replace_file(const std::wstring& path, const std::string& data)
{
	std::wstring tmp = path + L".tmp";
	HANDLE f = CreateFileW(tmp.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (f == INVALID_HANDLE_VALUE)
		return false;
	bool ok = write_all(f, data.data(), data.size());
	CloseHandle(f);
	return ok && MoveFileExW(tmp.c_str(), path.c_str(), MOVEFILE_REPLACE_EXISTING);
}

static void close_files()
{
	HANDLE* hs[] = { &g_w.lines, &g_w.runs, &g_w.frames, &g_w.keyframes };
	for (HANDLE* h : hs)
		if (*h != INVALID_HANDLE_VALUE) { CloseHandle(*h); *h = INVALID_HANDLE_VALUE; }
	g_w.ok = false;
}

static void do_open(Task& t)
{
	close_files();
	g_w = Rec();
	g_w.id = t.id;
	g_w.head = t.text;
	EnterCriticalSection(&g_cs);
	g_st_id = t.id;
	g_st_error.clear();
	g_st_folder.clear();
	LeaveCriticalSection(&g_cs);
	InterlockedExchange64(&g_st_bytes, 0);
	if (!is_dir(g_rc.root) && !CreateDirectoryW(g_rc.root.c_str(), nullptr)) {
		fail("could not create " + narrow(g_rc.root.c_str()));
		return;
	}
	std::wstring base = g_rc.root + L"\\" + widen(t.reason), dir = base;
	for (int n = 2; !CreateDirectoryW(dir.c_str(), nullptr); n++) {
		if (GetLastError() != ERROR_ALREADY_EXISTS || n > 99) {
			fail("could not create " + narrow(dir.c_str()));
			return;
		}
		dir = base + L"-" + std::to_wstring(n);
	}
	CreateDirectoryW((dir + L"\\keyframes").c_str(), nullptr);
	g_w.dir = dir;
	g_w.lines = open_append(dir + L"\\lines.jsonl");
	g_w.runs = open_append(dir + L"\\runs.jsonl");
	g_w.frames = open_append(dir + L"\\frames.bin");
	g_w.keyframes = open_append(dir + L"\\keyframes.jsonl");
	g_w.ok = g_w.lines != INVALID_HANDLE_VALUE && g_w.runs != INVALID_HANDLE_VALUE &&
	         g_w.frames != INVALID_HANDLE_VALUE && g_w.keyframes != INVALID_HANDLE_VALUE;
	if (!g_w.ok) {
		fail("could not create the files in " + narrow(dir.c_str()));
		close_files();
		return;
	}
	replace_file(dir + L"\\recording.json", "{" + g_w.head + ",\"state\":\"recording\"}\n");
	EnterCriticalSection(&g_cs);
	g_st_folder = narrow(dir.c_str());
	LeaveCriticalSection(&g_cs);
	plog("record: recording %u into %s", t.id, narrow(dir.c_str()).c_str());
}

static void do_keyframe(Task& t)
{
	std::string json;
	std::vector<uint8_t> blob;
	cap_serialize(*t.cap, &json, &blob);
	uint32_t frame = cap_frame_of(*t.cap);
	size_t draws = 0;
	{
		// The number of draws is the first member of "stats"; cheaper to keep than to look up.
		const char* p = strstr(json.c_str(), "\"stats\":{\"draws\":");
		if (p) draws = (size_t)strtoull(p + 17, nullptr, 10);
	}
	t.cap.reset();
	wchar_t name[64];
	swprintf_s(name, L"\\keyframes\\%u", frame);
	std::wstring base = g_w.dir + name;
	HANDLE f = CreateFileW((base + L".json").c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS,
	                       FILE_ATTRIBUTE_NORMAL, nullptr);
	bool ok = f != INVALID_HANDLE_VALUE && append(f, json.data(), json.size(), "a keyframe");
	if (f != INVALID_HANDLE_VALUE) CloseHandle(f);
	f = CreateFileW((base + L".bin").c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS,
	                FILE_ATTRIBUTE_NORMAL, nullptr);
	ok = ok && f != INVALID_HANDLE_VALUE && (blob.empty() || append(f, blob.data(), blob.size(), "a keyframe"));
	if (f != INVALID_HANDLE_VALUE) CloseHandle(f);
	if (!ok) {
		fail("could not write keyframe " + std::to_string(frame));
		return;
	}
	JsonW w;
	w.begin_obj();
	w.ku("frame", frame);
	w.ku("t_ms", t.t_ms);
	w.ks("reason", t.reason);
	if (!t.what.empty()) w.ks("what", t.what);
	w.ku("draws", draws);
	char rel[48];
	snprintf(rel, sizeof(rel), "keyframes/%u.json", frame);
	w.ks("json", rel);
	snprintf(rel, sizeof(rel), "keyframes/%u.bin", frame);
	w.ks("bin", rel);
	w.ku("json_bytes", json.size());
	w.ku("bin_bytes", blob.size());
	w.end_obj();
	w.s += '\n';
	append(g_w.keyframes, w.s.data(), w.s.size(), "keyframes.jsonl");
}

static void do_close(Task& t)
{
	std::wstring dir = g_w.dir;
	close_files();
	DeleteFileW((dir + L"\\open-runs.json").c_str());
	if (!replace_file(dir + L"\\recording.json", "{" + g_w.head + "," + t.text + ",\"state\":\"stopped\"}\n"))
		fail("could not finish recording.json");
	plog("record: recording %u stopped", t.id);
}

static void run_task(Task& t)
{
	if (t.type == T_OPEN) {
		do_open(t);
		return;
	}
	if (t.id != g_w.id || g_w.dir.empty())
		return;                                 // its folder could not be made: nothing to write into
	switch (t.type) {
	case T_LINES:  append(g_w.lines, t.text.data(), t.text.size(), "lines.jsonl"); break;
	case T_RUNS:   append(g_w.runs, t.text.data(), t.text.size(), "runs.jsonl"); break;
	case T_FRAMES: append(g_w.frames, t.bin.data(), t.bin.size(), "frames.bin"); break;
	case T_OPEN_RUNS:
		if (g_w.ok) replace_file(g_w.dir + L"\\open-runs.json", t.text);
		break;
	case T_KEYFRAME:
		if (g_w.ok) do_keyframe(t);
		break;
	case T_CLOSE:  do_close(t); break;
	}
}

static DWORD WINAPI writer_main(LPVOID)
{
	for (;;) {
		EnterCriticalSection(&g_cs);
		while (g_q.empty())
			SleepConditionVariableCS(&g_cv_work, &g_cs, INFINITE);
		Task t = std::move(g_q.front());
		g_q.pop_front();
		LeaveCriticalSection(&g_cs);
		run_task(t);
		t.cap.reset();
		EnterCriticalSection(&g_cs);
		g_done++;
		LeaveCriticalSection(&g_cs);
		WakeAllConditionVariable(&g_cv_idle);
	}
}
