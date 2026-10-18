// Slot cache: the folder per game version, and the thread that writes into it.
//
// The render thread hands over one finished record per draw it had not seen before (see
// "slot cache" in capture.cpp). Everything that touches a file happens here, on a thread of
// its own: working out the game version, loading what earlier sessions wrote, appending new
// records, saving the bytecode of shaders that were not saved yet.
#include "slotcache.h"
#include "registry.h"
#include "json.h"
#include <deque>
#include <unordered_set>
#include <ctype.h>
#include <string.h>

static CRITICAL_SECTION g_cs;
static CONDITION_VARIABLE g_cv_work, g_cv_idle;
static bool g_inited = false;
static SlotCacheConfig g_sc;                  // guarded by g_cs
static volatile LONG g_enable = 0, g_every = 30, g_skinned = 1, g_max_new = 256;
static volatile LONG g_gen = 0;
static HANDLE g_thread = nullptr;             // guarded by g_cs

struct Item { std::string rec, key; uint64_t vs, ps; };
static std::deque<Item> g_q;                  // guarded by g_cs
static uint64_t g_submitted = 0, g_done = 0;  // guarded by g_cs

// ---- writer thread only
static std::wstring g_root;                   // <probe dir>\<subdir>
static std::wstring g_dir;                    // <root>\<version key>
static uint64_t g_checked_ms = 0;
static HANDLE g_file = INVALID_HANDLE_VALUE;
static std::unordered_set<std::string> g_keys;
static std::unordered_set<uint64_t> g_saved_shaders;

// ---- status
static volatile LONG64 g_records = 0, g_dups = 0, g_shaders = 0, g_no_bytecode = 0;
static volatile LONG64 g_frames = 0, g_new_draws = 0, g_deferred = 0;
static volatile LONG64 g_cost_us = 0, g_last_cost_us = 0;
static std::string g_status_key, g_status_label, g_status_dir, g_err;   // guarded by g_cs
static uint64_t g_keys_total = 0;                                       // guarded by g_cs

static bool plain_name(const std::string& s)
{
	if (s.empty() || s.size() > 64 || s == "." || s == "..")
		return false;
	for (char ch : s)
		if (!(isalnum((unsigned char)ch) || ch == '-' || ch == '_' || ch == '.'))
			return false;
	return true;
}

static std::wstring widen(const std::string& s)
{
	if (s.empty()) return std::wstring();
	int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
	std::wstring w((size_t)n, L'\0');
	MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], n);
	return w;
}

static void set_error(const std::string& e)
{
	EnterCriticalSection(&g_cs);
	g_err = e;
	LeaveCriticalSection(&g_cs);
	plog("slot cache: %s", e.c_str());
}

void slot_init()
{
	InitializeCriticalSection(&g_cs);
	InitializeConditionVariable(&g_cv_work);
	InitializeConditionVariable(&g_cv_idle);
	g_inited = true;

	wchar_t path[MAX_PATH];
	swprintf_s(path, L"%s\\efmi_probe.ini", probe_dir());
	SlotCacheConfig c;
	c.enable = GetPrivateProfileIntW(L"SlotCache", L"enable", 1, path) != 0;
	c.every = (uint32_t)GetPrivateProfileIntW(L"SlotCache", L"every", 30, path);
	c.skinned_only = GetPrivateProfileIntW(L"SlotCache", L"skinned_only", 1, path) != 0;
	c.max_new = (uint32_t)GetPrivateProfileIntW(L"SlotCache", L"max_new", 256, path);
	wchar_t sub[128] = {};
	GetPrivateProfileStringW(L"SlotCache", L"dir", L"slot-cache", sub, 128, path);
	std::string s = narrow(sub);
	if (plain_name(s))
		c.subdir = s;
	slot_set_config(c);
	plog("slot cache: %s, one frame in %u, %s draws, folder %s", c.enable ? "on" : "off", c.every,
	     c.skinned_only ? "skinned" : "all", c.subdir.c_str());
}

void slot_get_config(SlotCacheConfig* out)
{
	EnterCriticalSection(&g_cs);
	*out = g_sc;
	LeaveCriticalSection(&g_cs);
}

bool slot_set_config(const SlotCacheConfig& cfg)
{
	if (!g_inited || !plain_name(cfg.subdir))
		return false;
	SlotCacheConfig c = cfg;
	if (c.every < 1) c.every = 1;
	if (c.every > 100000) c.every = 100000;
	if (c.max_new < 1) c.max_new = 1;
	if (c.max_new > 4096) c.max_new = 4096;
	EnterCriticalSection(&g_cs);
	const bool moved = g_sc.subdir != c.subdir;
	g_sc = c;
	LeaveCriticalSection(&g_cs);
	if (moved)
		InterlockedIncrement(&g_gen);   // another folder: what was seen is recorded there again
	InterlockedExchange(&g_every, (LONG)c.every);
	InterlockedExchange(&g_skinned, c.skinned_only ? 1 : 0);
	InterlockedExchange(&g_max_new, (LONG)c.max_new);
	InterlockedExchange(&g_enable, c.enable ? 1 : 0);
	return true;
}

bool slot_due(uint32_t frame)
{
	LONG every = g_every;
	return g_enable && every > 0 && frame % (uint32_t)every == 0;
}

bool slot_skinned_only() { return g_skinned != 0; }
uint32_t slot_max_new() { return (uint32_t)g_max_new; }
LONG slot_generation() { return g_gen; }

void slot_note_frame(double record_ms, uint32_t new_draws, uint32_t deferred)
{
	LONG64 us = (LONG64)(record_ms * 1000.0);
	InterlockedIncrement64(&g_frames);
	InterlockedExchangeAdd64(&g_new_draws, new_draws);
	InterlockedExchangeAdd64(&g_deferred, deferred);
	InterlockedExchangeAdd64(&g_cost_us, us);
	InterlockedExchange64(&g_last_cost_us, us);
}

// ================================================================ the game version
static void fnv_add(uint64_t* h, const void* p, size_t n)
{
	const uint8_t* b = (const uint8_t*)p;
	for (size_t i = 0; i < n; i++) {
		*h ^= b[i];
		*h *= 1099511628211ull;
	}
}

static bool read_file(const std::wstring& path, std::string* out, uint64_t cap)
{
	HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
	                       nullptr, OPEN_EXISTING, 0, nullptr);
	if (f == INVALID_HANDLE_VALUE)
		return false;
	LARGE_INTEGER sz = {};
	bool ok = GetFileSizeEx(f, &sz) && (uint64_t)sz.QuadPart <= cap;
	if (ok) {
		out->resize((size_t)sz.QuadPart);
		size_t at = 0;
		while (at < out->size()) {
			DWORD got = 0, want = (DWORD)((out->size() - at) > (16u << 20) ? (16u << 20) : (out->size() - at));
			if (!ReadFile(f, &(*out)[at], want, &got, nullptr) || !got) { ok = false; break; }
			at += got;
		}
		out->resize(at);
	}
	CloseHandle(f);
	return ok;
}

static bool file_stamp(const std::wstring& path, uint64_t* size, uint64_t* unix_ms)
{
	WIN32_FILE_ATTRIBUTE_DATA a;
	if (!GetFileAttributesExW(path.c_str(), GetFileExInfoStandard, &a))
		return false;
	*size = ((uint64_t)a.nFileSizeHigh << 32) | a.nFileSizeLow;
	uint64_t ft = ((uint64_t)a.ftLastWriteTime.dwHighDateTime << 32) | a.ftLastWriteTime.dwLowDateTime;
	*unix_ms = ft > 116444736000000000ull ? (ft - 116444736000000000ull) / 10000 : 0;
	return true;
}

// Unity keeps the application version as a plain string in globalgamemanagers. Version-like
// strings whose first number is in the thousands are Unity's own (2021.3.34f5, 6000.0.x).
static std::string find_version_label(const std::string& d)
{
	const size_t n = d.size();
	size_t i = 0;
	while (i < n) {
		if (!isdigit((unsigned char)d[i]) || (i && (isalnum((unsigned char)d[i - 1]) || d[i - 1] == '.'))) {
			i++;
			continue;
		}
		size_t j = i;
		int parts = 0;
		uint64_t first = 0;
		for (;;) {
			size_t k = j;
			uint64_t v = 0;
			while (k < n && isdigit((unsigned char)d[k]) && k - j < 9)
				v = v * 10 + (uint64_t)(d[k++] - '0');
			if (!parts) first = v;
			parts++;
			j = k;
			if (parts < 4 && j + 1 < n && d[j] == '.' && isdigit((unsigned char)d[j + 1])) {
				j++;
				continue;
			}
			break;
		}
		size_t end = j;
		while (end < n && end - j < 8 && isalnum((unsigned char)d[end]))
			end++;
		bool longer = end < n && isalnum((unsigned char)d[end]);
		if (parts >= 3 && first < 1000 && !longer)
			return d.substr(i, end - i);
		i = end > i ? end : i + 1;
	}
	return std::string();
}

struct VersionInfo { std::string key, label, json; };

// The version key is the client version Unity reports plus a fingerprint of the files that
// change with every update, the resource indexes included: a resource update can move texture
// slots while the client files stay as they were.
static VersionInfo compute_version()
{
	wchar_t exe_buf[MAX_PATH] = {};
	GetModuleFileNameW(nullptr, exe_buf, MAX_PATH);
	std::wstring exe = exe_buf;
	size_t slash = exe.find_last_of(L"\\/");
	std::wstring dir = slash == std::wstring::npos ? L"." : exe.substr(0, slash);
	std::wstring name = slash == std::wstring::npos ? exe : exe.substr(slash + 1);
	size_t dot = name.find_last_of(L'.');
	std::wstring stem = dot == std::wstring::npos ? name : name.substr(0, dot);
	std::wstring data = dir + L"\\" + stem + L"_Data";

	uint64_t h = 14695981039346656037ull;
	JsonW inputs;
	inputs.begin_arr();
	const wchar_t* contents[] = { L"\\Persistent\\index_main.json", L"\\StreamingAssets\\index_main.json" };
	for (const wchar_t* rel : contents) {
		std::string bytes;
		std::wstring p = data + rel;
		if (!read_file(p, &bytes, 64ull << 20))
			continue;
		std::string label = narrow((stem + L"_Data" + rel).c_str());
		fnv_add(&h, label.data(), label.size());
		fnv_add(&h, bytes.data(), bytes.size());
		inputs.begin_obj(); inputs.ks("file", label); inputs.ku("size", bytes.size()); inputs.kb("content", true); inputs.end_obj();
	}
	struct Stamped { std::wstring path; std::wstring rel; };
	Stamped stamped[] = { { exe, name }, { dir + L"\\GameAssembly.dll", L"GameAssembly.dll" },
	                      { data + L"\\globalgamemanagers", stem + L"_Data\\globalgamemanagers" } };
	for (const Stamped& s : stamped) {
		uint64_t size = 0, ms = 0;
		if (!file_stamp(s.path, &size, &ms))
			continue;
		std::string label = narrow(s.rel.c_str());
		fnv_add(&h, label.data(), label.size());
		fnv_add(&h, &size, sizeof(size));
		fnv_add(&h, &ms, sizeof(ms));
		inputs.begin_obj(); inputs.ks("file", label); inputs.ku("size", size); inputs.ku("modified_unix_ms", ms); inputs.end_obj();
	}
	inputs.end_arr();

	std::string ggm, label;
	if (read_file(data + L"\\globalgamemanagers", &ggm, 64ull << 20))
		label = find_version_label(ggm);
	std::string clean;
	for (char ch : label)
		if (isalnum((unsigned char)ch) || ch == '.' || ch == '-' || ch == '_')
			clean += ch;
	if (clean.size() > 32) clean.resize(32);
	if (clean.empty()) clean = "unknown";

	char fp[24];
	snprintf(fp, sizeof(fp), "%012llx", (unsigned long long)(h >> 16));
	VersionInfo v;
	v.label = clean;
	v.key = clean + "+" + fp;
	JsonW w;
	w.begin_obj();
	w.ks("key", v.key);
	w.ks("label", v.label);
	w.ks("game", narrow(exe.c_str()));
	w.kb("unity_data_folder", GetFileAttributesW(data.c_str()) != INVALID_FILE_ATTRIBUTES);
	w.key("fingerprint_inputs"); w.raw(inputs.s);
	w.ks("probe", PROBE_VERSION);
	FILETIME now;
	GetSystemTimeAsFileTime(&now);
	uint64_t ft = ((uint64_t)now.dwHighDateTime << 32) | now.dwLowDateTime;
	w.ku("created_unix_ms", (ft - 116444736000000000ull) / 10000);
	w.end_obj();
	v.json = w.s;
	return v;
}

// ================================================================ the files
static bool write_new_file(const std::wstring& path, const void* data, size_t size, bool* existed)
{
	*existed = false;
	HANDLE f = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (f == INVALID_HANDLE_VALUE) {
		*existed = GetLastError() == ERROR_FILE_EXISTS;
		return false;
	}
	DWORD w = 0;
	bool ok = WriteFile(f, data, (DWORD)size, &w, nullptr) && w == size;
	CloseHandle(f);
	if (!ok)
		DeleteFileW(path.c_str());
	return ok;
}

static bool g_tail_complete = true;           // draws.jsonl is empty or ends with a line break

// Records of probe 0.1.7 were keyed by position as well (first index, base vertex, sizes) and carry
// no "fmt"; they are moved aside rather than mixed with records keyed by content.
static void set_aside_old_records()
{
	std::wstring path = g_dir + L"\\draws.jsonl";
	HANDLE f = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
	if (f == INVALID_HANDLE_VALUE)
		return;
	char head[64] = {};
	DWORD got = 0;
	ReadFile(f, head, sizeof(head) - 1, &got, nullptr);
	CloseHandle(f);
	if (!got || strncmp(head, "{\"fmt\":2,", 9) == 0)
		return;
	for (int n = 1; n < 100; n++) {
		wchar_t name[64];
		swprintf_s(name, n == 1 ? L"\\draws-fmt1.jsonl" : L"\\draws-fmt1-%d.jsonl", n);
		if (MoveFileExW(path.c_str(), (g_dir + name).c_str(), 0)) {
			plog("slot cache: records of an older format moved to %s", narrow((g_dir + name).c_str()).c_str());
			return;
		}
		if (GetLastError() != ERROR_ALREADY_EXISTS && GetLastError() != ERROR_FILE_EXISTS)
			break;
	}
	set_error("cannot move the records of an older format out of draws.jsonl");
}

static void load_existing()
{
	std::string text;
	g_tail_complete = true;
	if (read_file(g_dir + L"\\draws.jsonl", &text, 512ull << 20)) {
		g_tail_complete = text.empty() || text.back() == '\n';
		const char* tag = "\"key\":\"";
		size_t at = 0;
		while ((at = text.find(tag, at)) != std::string::npos) {
			at += strlen(tag);
			size_t end = text.find('"', at);
			if (end == std::string::npos)
				break;
			g_keys.insert(text.substr(at, end - at));
			at = end;
		}
	}
	WIN32_FIND_DATAW fd;
	HANDLE fh = FindFirstFileW((g_dir + L"\\shaders\\*.bin").c_str(), &fd);
	if (fh != INVALID_HANDLE_VALUE) {
		do {
			std::string n = narrow(fd.cFileName);
			if (n.size() >= 16)
				g_saved_shaders.insert(_strtoui64(n.substr(0, 16).c_str(), nullptr, 16));
		} while (FindNextFileW(fh, &fd));
		FindClose(fh);
	}
}

// Picks the folder for the version the game is at now; looks again at most once a minute.
static void ensure_folder(const std::string& subdir)
{
	std::wstring root = std::wstring(probe_dir()) + L"\\" + widen(subdir);
	uint64_t now = now_ms();
	if (!g_dir.empty() && root == g_root && now - g_checked_ms < 60000)
		return;
	g_checked_ms = now;
	VersionInfo v = compute_version();
	std::wstring dir = root + L"\\" + widen(v.key);
	if (dir == g_dir && g_file != INVALID_HANDLE_VALUE)
		return;

	// Another version in the same place: the render thread has to look at everything again. A
	// change of place was announced by slot_set_config already.
	const bool new_version = !g_dir.empty() && root == g_root;
	if (g_file != INVALID_HANDLE_VALUE) {
		CloseHandle(g_file);
		g_file = INVALID_HANDLE_VALUE;
	}
	g_keys.clear();
	g_saved_shaders.clear();
	g_root = root;
	g_dir = dir;
	CreateDirectoryW(root.c_str(), nullptr);
	CreateDirectoryW(dir.c_str(), nullptr);
	CreateDirectoryW((dir + L"\\shaders").c_str(), nullptr);
	bool existed = false;
	write_new_file(dir + L"\\version.json", v.json.data(), v.json.size(), &existed);
	set_aside_old_records();
	load_existing();
	g_file = CreateFileW((dir + L"\\draws.jsonl").c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS,
	                     FILE_ATTRIBUTE_NORMAL, nullptr);
	if (g_file != INVALID_HANDLE_VALUE && !g_tail_complete) {
		// The last session ended in the middle of a line: what follows starts on a line of its own.
		DWORD w = 0;
		WriteFile(g_file, "\n", 1, &w, nullptr);
	}
	EnterCriticalSection(&g_cs);
	g_status_key = v.key;
	g_status_label = v.label;
	g_status_dir = narrow(dir.c_str());
	g_keys_total = g_keys.size();
	LeaveCriticalSection(&g_cs);
	if (g_file == INVALID_HANDLE_VALUE)
		set_error("cannot open " + narrow(dir.c_str()) + "\\draws.jsonl");
	plog("slot cache: version %s, folder %s, %zu records and %zu shaders already there", v.key.c_str(),
	     narrow(dir.c_str()).c_str(), g_keys.size(), g_saved_shaders.size());
	if (new_version)
		InterlockedIncrement(&g_gen);
}

static void save_shader(uint64_t hash)
{
	if (!hash || g_saved_shaders.count(hash))
		return;
	std::vector<uint8_t> code;
	std::string type;
	if (!reg_get_bytecode(hash, &code, &type) || code.empty() || type.empty()) {
		InterlockedIncrement64(&g_no_bytecode);
		g_saved_shaders.insert(hash);      // asked once; the store keeps nothing that arrives later
		return;
	}
	wchar_t name[64];
	swprintf_s(name, L"\\shaders\\%016llx-%hs.bin", (unsigned long long)hash, type.c_str());
	bool existed = false;
	if (write_new_file(g_dir + name, code.data(), code.size(), &existed) || existed) {
		g_saved_shaders.insert(hash);
		if (!existed)
			InterlockedIncrement64(&g_shaders);
	} else {
		set_error("cannot write shader " + narrow(name));
	}
}

static void write_item(const Item& it)
{
	if (g_file == INVALID_HANDLE_VALUE)
		return;
	if (g_keys.insert(it.key).second) {
		std::string line = it.rec;
		line += '\n';
		DWORD w = 0;
		if (WriteFile(g_file, line.data(), (DWORD)line.size(), &w, nullptr) && w == line.size())
			InterlockedIncrement64(&g_records);
		else
			set_error("cannot append to draws.jsonl");
	} else {
		InterlockedIncrement64(&g_dups);
	}
	save_shader(it.vs);
	save_shader(it.ps);
}

static DWORD WINAPI writer_main(void*)
{
	for (;;) {
		std::deque<Item> batch;
		EnterCriticalSection(&g_cs);
		while (g_q.empty())
			SleepConditionVariableCS(&g_cv_work, &g_cs, INFINITE);
		batch.swap(g_q);
		std::string subdir = g_sc.subdir;
		LeaveCriticalSection(&g_cs);

		ensure_folder(subdir);
		for (const Item& it : batch)
			write_item(it);

		EnterCriticalSection(&g_cs);
		g_done += batch.size();
		g_keys_total = g_keys.size();
		LeaveCriticalSection(&g_cs);
		WakeAllConditionVariable(&g_cv_idle);
	}
}

static void start_writer_locked()
{
	if (!g_thread) {
		g_thread = CreateThread(nullptr, 0, writer_main, nullptr, 0, nullptr);
		if (!g_thread)
			plog("slot cache: cannot start the writer thread (%lu)", GetLastError());
	}
}

void slot_submit(std::string&& record, std::string&& key, uint64_t vs, uint64_t ps)
{
	if (!g_inited)
		return;
	EnterCriticalSection(&g_cs);
	start_writer_locked();
	if (g_thread) {
		g_q.push_back({ std::move(record), std::move(key), vs, ps });
		g_submitted++;
	}
	LeaveCriticalSection(&g_cs);
	WakeConditionVariable(&g_cv_work);
}

bool slot_flush(uint32_t timeout_ms)
{
	if (!g_inited)
		return false;
	uint64_t deadline = now_ms() + timeout_ms;
	EnterCriticalSection(&g_cs);
	uint64_t target = g_submitted;
	while (g_done < target) {
		uint64_t now = now_ms();
		if (now >= deadline)
			break;
		SleepConditionVariableCS(&g_cv_idle, &g_cs, (DWORD)(deadline - now));
	}
	bool ok = g_done >= target;
	LeaveCriticalSection(&g_cs);
	return ok;
}

void slot_status_object(std::string* out)
{
	JsonW w;
	w.begin_obj();
	if (!g_inited) {
		w.kb("enabled", false);
		w.end_obj();
		*out = w.s;
		return;
	}
	EnterCriticalSection(&g_cs);
	w.kb("enabled", g_sc.enable);
	w.ku("every", g_sc.every);
	w.kb("skinned_only", g_sc.skinned_only);
	w.ku("max_new", g_sc.max_new);
	w.ks("subdir", g_sc.subdir);
	w.ks("version", g_status_key);
	w.ks("label", g_status_label);
	w.ks("folder", g_status_dir);
	w.ku("records_in_folder", g_keys_total);
	w.ku("pending", g_submitted - g_done);
	if (!g_err.empty()) w.ks("error", g_err);
	LeaveCriticalSection(&g_cs);
	w.ku("frames_looked_at", (uint64_t)g_frames);
	w.ku("new_draws", (uint64_t)g_new_draws);
	w.ku("deferred", (uint64_t)g_deferred);
	w.ku("records_written", (uint64_t)g_records);
	w.ku("duplicates", (uint64_t)g_dups);
	w.ku("shaders_written", (uint64_t)g_shaders);
	w.ku("shaders_without_bytecode", (uint64_t)g_no_bytecode);
	w.kd("record_ms_total", (double)g_cost_us / 1000.0);
	w.kd("record_ms_last", (double)g_last_cost_us / 1000.0);
	w.end_obj();
	*out = w.s;
}
