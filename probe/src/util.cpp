#include "common.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

Settings g_cfg;
volatile LONG g_frame_no = 0;

static CRITICAL_SECTION g_log_cs;
static HANDLE g_log_file = INVALID_HANDLE_VALUE;
static wchar_t g_probe_dir[MAX_PATH];
static bool g_log_ready = false;

const wchar_t* probe_dir() { return g_probe_dir; }

uint64_t now_ms() { return GetTickCount64(); }

std::string narrow(const wchar_t* w)
{
	if (!w) return std::string();
	int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
	if (n <= 1) return std::string();
	std::string s((size_t)n - 1, '\0');
	WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
	return s;
}

void log_init(HMODULE self)
{
	InitializeCriticalSection(&g_log_cs);
	GetModuleFileNameW(self, g_probe_dir, MAX_PATH);
	wchar_t* slash = wcsrchr(g_probe_dir, L'\\');
	if (slash) *slash = 0;

	wchar_t path[MAX_PATH];
	swprintf_s(path, L"%s\\efmi_probe.log", g_probe_dir);
	g_log_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_DELETE, nullptr,
	                         CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
	if (g_log_file == INVALID_HANDLE_VALUE) {
		wchar_t tmp[MAX_PATH];
		GetTempPathW(MAX_PATH, tmp);
		swprintf_s(path, L"%sefmi_probe.log", tmp);
		g_log_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_DELETE, nullptr,
		                         CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
	}
	g_log_ready = true;
}

void plog(const char* fmt, ...)
{
	if (!g_log_ready || g_log_file == INVALID_HANDLE_VALUE)
		return;
	char buf[2048];
	SYSTEMTIME st;
	GetLocalTime(&st);
	int n = sprintf_s(buf, "%02d:%02d:%02d.%03d [%5lu] ", st.wHour, st.wMinute, st.wSecond,
	                  st.wMilliseconds, GetCurrentThreadId());
	va_list ap;
	va_start(ap, fmt);
	int m = vsnprintf(buf + n, sizeof(buf) - n - 2, fmt, ap);
	va_end(ap);
	if (m < 0) m = 0;
	n += (m > (int)sizeof(buf) - n - 2) ? (int)sizeof(buf) - n - 2 : m;
	buf[n++] = '\r';
	buf[n++] = '\n';
	EnterCriticalSection(&g_log_cs);
	DWORD written;
	WriteFile(g_log_file, buf, (DWORD)n, &written, nullptr);
	LeaveCriticalSection(&g_log_cs);
}

// ---------------------------------------------------------------- settings
static int ini_int(const wchar_t* file, const wchar_t* sec, const wchar_t* key, int def)
{
	return (int)GetPrivateProfileIntW(sec, key, def, file);
}

void settings_load()
{
	wchar_t path[MAX_PATH];
	swprintf_s(path, L"%s\\efmi_probe.ini", g_probe_dir);
	if (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES)
		return;
	g_cfg.keep_bytecode   = ini_int(path, L"Probe", L"keep_bytecode", 1) != 0;
	g_cfg.bytecode_cap_mb = (uint32_t)ini_int(path, L"Probe", L"bytecode_cap_mb", 256);
	g_cfg.arena_chunk_mb  = (uint32_t)ini_int(path, L"Probe", L"arena_chunk_mb", 8);
	g_cfg.arena_cap_mb    = (uint32_t)ini_int(path, L"Probe", L"arena_cap_mb", 768);
	g_cfg.max_draws       = (uint32_t)ini_int(path, L"Probe", L"max_draws", 60000);
	g_cfg.stack_origin    = ini_int(path, L"Probe", L"stack_origin", 1) != 0;
	plog("settings: probe ini loaded");
}

// Read-only look at XXMI's d3dx.ini: only the options that change hash values.
void settings_load_xxmi(const wchar_t* ini)
{
	if (GetFileAttributesW(ini) == INVALID_FILE_ATTRIBUTES) {
		plog("settings: d3dx.ini not found next to XXMI, keeping defaults");
		return;
	}
	wchar_t buf[128];
	g_cfg.texture_hash = ini_int(ini, L"Rendering", L"texture_hash", 0);
	g_cfg.track_region_hashes = ini_int(ini, L"Rendering", L"track_region_hashes", 0) != 0;

	GetPrivateProfileStringW(L"Rendering", L"shader_hash", L"3dmigoto", buf, _countof(buf), ini);
	if (!_wcsicmp(buf, L"embedded")) g_cfg.shader_hash = 1;
	else if (!_wcsicmp(buf, L"bytecode")) g_cfg.shader_hash = 2;
	else g_cfg.shader_hash = 0;

	GetPrivateProfileStringW(L"Device", L"get_resolution_from", L"", buf, _countof(buf), ini);
	g_cfg.res_from_swap_chain = !_wcsicmp(buf, L"swap_chain");

	plog("settings: d3dx.ini texture_hash=%d track_region_hashes=%d shader_hash=%d res_from_swap_chain=%d",
	     g_cfg.texture_hash, (int)g_cfg.track_region_hashes, g_cfg.shader_hash, (int)g_cfg.res_from_swap_chain);
	if (g_cfg.shader_hash == 2)
		plog("settings: WARNING shader_hash=bytecode is not reproduced, shader hashes will not match XXMI");
}

// ---------------------------------------------------------------- modules
bool module_from_address(const void* addr, ModuleRange* out)
{
	HMODULE h = nullptr;
	if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
	                        (LPCWSTR)addr, &h) || !h)
		return false;
	const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)h;
	const IMAGE_NT_HEADERS* nt = (const IMAGE_NT_HEADERS*)((const uint8_t*)h + dos->e_lfanew);
	out->base = (uintptr_t)h;
	out->end = out->base + nt->OptionalHeader.SizeOfImage;
	out->path[0] = 0;
	GetModuleFileNameW(h, out->path, MAX_PATH);
	return true;
}

bool ptr_in(const ModuleRange& m, const void* p)
{
	return m.base && (uintptr_t)p >= m.base && (uintptr_t)p < m.end;
}

bool is_exec_ptr(const void* p)
{
	MEMORY_BASIC_INFORMATION mbi;
	if (!p || !VirtualQuery(p, &mbi, sizeof(mbi)))
		return false;
	if (mbi.State != MEM_COMMIT)
		return false;
	const DWORD exec = PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
	return (mbi.Protect & exec) != 0 && !(mbi.Protect & PAGE_GUARD);
}

bool is_system_module_path(const wchar_t* path)
{
	wchar_t win[MAX_PATH];
	UINT n = GetWindowsDirectoryW(win, MAX_PATH);
	if (!n || !path)
		return false;
	return _wcsnicmp(path, win, n) == 0 && (path[n] == L'\\' || path[n] == L'/');
}
