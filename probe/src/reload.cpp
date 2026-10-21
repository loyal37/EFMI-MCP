#include "reload.h"
#include "hooks.h"
#include "json.h"
#include <string.h>
#include <wctype.h>

// XXMI reads the keyboard at the start of its Present wrapper and then calls
// the real Present, where this probe sees the frame. In between it only draws
// its overlay, far quicker than this; a reload takes many times longer.
static const double     k_poll_to_present_ms = 10.0;

struct KeySpec {
	int         vk;
	const char* name;      // as reported and logged
	const char* action;    // what the client asked for
	const char* before;    // "... before <this>", in messages
	const char* nothing;   // why the key is pointless without XXMI
};

static const KeySpec k_reload = { VK_F10, "F10", "reload", "the reload", "there is nothing to reload" };
// Switching all mods uses the installation's own key: EFMI's F11, ZZMI's F6.
static const KeySpec k_toggle_efmi = { VK_F11, "F11", "toggle_mods", "switching mods", "there are no mods to switch" };
static const KeySpec k_toggle_zzmi = { VK_F6, "F6", "toggle_mods", "switching mods", "there are no mods to switch" };

static void* volatile   g_output_window = nullptr;
static volatile LONG    g_armed = 0;
static volatile LONG    g_presents = 0;       // frames presented while armed
static volatile LONG64  g_present_qpc = 0;    // time of the latest one
static HANDLE           g_frame_evt = nullptr;
static double           g_ms_per_tick = 0;

void reload_set_output_window(HWND hwnd)
{
	InterlockedExchangePointer(&g_output_window, hwnd);
}

void reload_on_present()
{
	if (!g_armed)
		return;
	LARGE_INTEGER t;
	QueryPerformanceCounter(&t);
	InterlockedExchange64(&g_present_qpc, t.QuadPart);
	InterlockedIncrement(&g_presents);
	SetEvent(g_frame_evt);
}

void reload_process_info(bool* elevated, uint32_t* integrity_rid)
{
	*elevated = false;
	*integrity_rid = 0;
	HANDLE tok = nullptr;
	if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok))
		return;
	TOKEN_ELEVATION te = {};
	DWORD len = 0;
	if (GetTokenInformation(tok, TokenElevation, &te, sizeof(te), &len))
		*elevated = te.TokenIsElevated != 0;
	BYTE buf[128];
	if (GetTokenInformation(tok, TokenIntegrityLevel, buf, sizeof(buf), &len)) {
		PSID sid = ((TOKEN_MANDATORY_LABEL*)buf)->Label.Sid;
		*integrity_rid = *GetSidSubAuthority(sid, (DWORD)(*GetSidSubAuthorityCount(sid) - 1));
	}
	CloseHandle(tok);
}

// ---------------------------------------------------------------- helpers
static LONGLONG ticks()
{
	LARGE_INTEGER t;
	QueryPerformanceCounter(&t);
	return t.QuadPart;
}

static double ms_between(LONGLONG a, LONGLONG b)
{
	return (double)(b - a) * g_ms_per_tick;
}

static DWORD window_pid(HWND h)
{
	DWORD pid = 0;
	if (h)
		GetWindowThreadProcessId(h, &pid);
	return pid;
}

// The condition XXMI applies before it looks at the keyboard (check_foreground_window).
static bool foreground_is_ours()
{
	return window_pid(GetForegroundWindow()) == GetCurrentProcessId();
}

// Modifiers that are down and, when `key` is given, that key itself.
static std::string held_keys(const KeySpec* key)
{
	static const struct { int vk; const char* name; } modifiers[] = {
		{ VK_SHIFT, "Shift" }, { VK_CONTROL, "Ctrl" }, { VK_MENU, "Alt" },
		{ VK_LWIN, "Left Win" }, { VK_RWIN, "Right Win" },
	};
	std::string out;
	auto add = [&out](int vk, const char* name) {
		if (GetAsyncKeyState(vk) & 0x8000) {
			if (!out.empty()) out += ", ";
			out += name;
		}
	};
	for (const auto& k : modifiers)
		add(k.vk, k.name);
	if (key)
		add(key->vk, key->name);
	return out;
}

struct FindWindowCtx { HWND best; LONG area; };

static BOOL CALLBACK find_window_cb(HWND h, LPARAM p)
{
	FindWindowCtx* c = (FindWindowCtx*)p;
	if (window_pid(h) != GetCurrentProcessId() || !IsWindowVisible(h) || GetWindow(h, GW_OWNER))
		return TRUE;
	RECT rc;
	if (!GetClientRect(h, &rc))
		return TRUE;
	LONG area = (rc.right - rc.left) * (rc.bottom - rc.top);
	if (area > c->area) {
		c->area = area;
		c->best = h;
	}
	return TRUE;
}

static HWND game_window()
{
	HWND h = (HWND)g_output_window;
	if (h && IsWindow(h) && window_pid(h) == GetCurrentProcessId()) {
		HWND root = GetAncestor(h, GA_ROOT);
		return root ? root : h;
	}
	// A swap chain without an output window: take the largest top-level window.
	FindWindowCtx c = {};
	EnumWindows(find_window_cb, (LPARAM)&c);
	return c.best;
}

// Lower case, single spaces, no trailing comment: "no_modifiers vk_f10".
static std::wstring normalized(const wchar_t* text)
{
	std::wstring norm;
	bool space = false;
	for (const wchar_t* p = text; *p && *p != L';'; p++) {
		if (*p == L' ' || *p == L'\t') {
			space = !norm.empty();
			continue;
		}
		if (space) {
			norm += L' ';
			space = false;
		}
		norm += (wchar_t)towlower(*p);
	}
	return norm;
}

static std::wstring binding(const wchar_t* ini, const wchar_t* section, const wchar_t* key)
{
	wchar_t buf[128];
	GetPrivateProfileStringW(section, key, L"", buf, _countof(buf), ini);
	return normalized(buf);
}

// A file of the XXMI installation that is loaded in this process.
static bool xxmi_file(const wchar_t* relative, wchar_t (&out)[MAX_PATH])
{
	wcscpy_s(out, g_xxmi.path);
	wchar_t* slash = wcsrchr(out, L'\\');
	if (!slash)
		return false;
	slash[1] = 0;
	return wcscat_s(out, relative) == 0;
}

static bool xxmi_reloads_on_f10()
{
	wchar_t ini[MAX_PATH];
	return xxmi_file(L"d3dx.ini", ini)
	    && binding(ini, L"Hunting", L"reload_config") == L"no_modifiers vk_f10"
	    && binding(ini, L"Hunting", L"reload_fixes") == L"no_modifiers vk_f10";
}

// The name of a [Hunting] function that the key alone would trigger too, i.e.
// one bound to it without requiring a modifier or a second key. Empty if none.
static std::string hunting_function_on_plain_key(const wchar_t* ini, const wchar_t* vk)
{
	static wchar_t section[32767];   // the most the API returns; IPC thread only
	DWORD n = GetPrivateProfileSectionW(L"Hunting", section, _countof(section), ini);
	for (const wchar_t* line = section; n && *line; line += wcslen(line) + 1) {
		std::wstring text = normalized(line);
		size_t eq = text.find(L'=');
		if (eq == std::wstring::npos)
			continue;
		bool has_key = false, needs_more = false;
		size_t at = eq + 1;
		while (at < text.size()) {
			size_t end = text.find(L' ', at);
			if (end == std::wstring::npos)
				end = text.size();
			std::wstring token = text.substr(at, end - at);
			// "VK_F6" and "F6" name the same key
			if (token == vk || L"vk_" + token == vk)
				has_key = true;
			else if (!token.empty() && token.compare(0, 3, L"no_") != 0)
				needs_more = true;      // ctrl, shift, alt or another key
			at = end + 1;
		}
		if (has_key && !needs_more) {
			std::wstring name = text.substr(0, eq);
			while (!name.empty() && name.back() == L' ')
				name.pop_back();
			return narrow(name.c_str());
		}
	}
	return std::string();
}

static bool exists(const wchar_t* path)
{
	return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

// The installation's own binding for switching all mods, and nothing else in
// XXMI that its key alone would set off. EFMI binds F11 in its KeyBindings.ini,
// ZZMI binds F6 in its main.ini; any other installation, or a changed binding,
// is refused rather than guessed.
static const KeySpec* install_toggle_key(std::string* why)
{
	wchar_t ini[MAX_PATH];
	const KeySpec* key = nullptr;
	const wchar_t* vk = nullptr;
	if (xxmi_file(L"Core\\EFMI\\KeyBindings.ini", ini) && exists(ini)) {
		if (binding(ini, L"KeyToggleMods", L"key") != L"no_modifiers vk_f11"
		    || binding(ini, L"KeyToggleMods", L"run") != L"commandlisttogglemods") {
			*why = "Core\\EFMI\\KeyBindings.ini does not have [KeyToggleMods] with 'Key = no_modifiers VK_F11' "
			       "and 'run = CommandListToggleMods'. No key was sent.";
			return nullptr;
		}
		key = &k_toggle_efmi;
		vk = L"vk_f11";
	} else if (xxmi_file(L"Core\\ZZMI\\main.ini", ini) && exists(ini)) {
		std::wstring k = binding(ini, L"KeyToggleMods", L"key");
		if ((k != L"no_modifiers f6" && k != L"no_modifiers vk_f6")
		    || binding(ini, L"KeyToggleMods", L"$costume_mods") != L"0, 1"
		    || binding(ini, L"KeyToggleMods", L"type") != L"cycle") {
			*why = "Core\\ZZMI\\main.ini does not have [KeyToggleMods] with 'Key = no_modifiers F6', "
			       "'$costume_mods = 0, 1' and 'type = cycle'. No key was sent.";
			return nullptr;
		}
		key = &k_toggle_zzmi;
		vk = L"vk_f6";
	} else {
		*why = "The loaded XXMI is neither EFMI (Core\\EFMI\\KeyBindings.ini) nor ZZMI (Core\\ZZMI\\main.ini), "
		       "so its key for switching mods is not known. No key was sent.";
		return nullptr;
	}
	if (!xxmi_file(L"d3dx.ini", ini) || !exists(ini)) {
		*why = "d3dx.ini of the loaded XXMI was not found. No key was sent.";
		return nullptr;
	}
	std::string other = hunting_function_on_plain_key(ini, vk);
	if (!other.empty()) {
		*why = "d3dx.ini binds [Hunting] " + other + " to " + key->name + " without a modifier, so " + key->name
		     + " would trigger it as well. No key was sent.";
		return nullptr;
	}
	return key;
}

static bool wait_foreground(DWORD ms)
{
	ULONGLONG end = GetTickCount64() + ms;
	while (!foreground_is_ours()) {
		if (GetTickCount64() >= end)
			return false;
		Sleep(5);
	}
	return true;
}

static const char* bring_to_front(HWND target)
{
	SetForegroundWindow(target);
	if (wait_foreground(250))
		return "direct";
	// Windows refuses a background process unless it shares the input state of
	// the thread that owns the foreground window. Attach for this one call.
	HWND fg = GetForegroundWindow();
	DWORD tid = fg ? GetWindowThreadProcessId(fg, nullptr) : 0;
	if (tid && tid != GetCurrentThreadId() && !IsHungAppWindow(fg)) {
		MSG msg;
		PeekMessageW(&msg, nullptr, WM_USER, WM_USER, PM_NOREMOVE);   // gives this thread an input queue
		if (AttachThreadInput(GetCurrentThreadId(), tid, TRUE)) {
			SetForegroundWindow(target);
			BringWindowToTop(target);
			AttachThreadInput(GetCurrentThreadId(), tid, FALSE);
			if (wait_foreground(400))
				return "attach_input";
		}
	}
	return nullptr;
}

// True once more than `seen` frames were presented since the press was armed.
static bool wait_present(LONG seen, DWORD ms)
{
	ULONGLONG end = GetTickCount64() + ms;
	while (g_presents <= seen) {
		ULONGLONG now = GetTickCount64();
		if (now >= end)
			return false;
		WaitForSingleObject(g_frame_evt, (DWORD)((end - now) < 20 ? (end - now) : 20));
	}
	return true;
}

static UINT send_key(int vk, bool up, DWORD* error)
{
	INPUT in = {};
	in.type = INPUT_KEYBOARD;
	in.ki.wVk = (WORD)vk;
	in.ki.wScan = (WORD)MapVirtualKeyW((UINT)vk, MAPVK_VK_TO_VSC);
	in.ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;
	SetLastError(0);
	UINT n = SendInput(1, &in, sizeof(in));
	*error = GetLastError();
	return n;
}

// ---------------------------------------------------------------- the press
const char* key_press_run(const KeyPressRequest& rq, JsonW& w, std::string* message)
{
	std::string toggle_why;
	const KeySpec* toggle = rq.key == GameKey::ToggleMods ? install_toggle_key(&toggle_why) : nullptr;
	const KeySpec& key = rq.key != GameKey::ToggleMods ? k_reload : toggle ? *toggle : k_toggle_efmi;
	const std::string name = key.name;
	const char* code = "ok";
	auto fail = [&](const char* c, const std::string& m) {
		if (!strcmp(code, "ok")) {   // the first problem is the one worth reporting
			code = c;
			*message = m;
		}
	};

	if (!g_ms_per_tick) {
		LARGE_INTEGER f;
		QueryPerformanceFrequency(&f);
		g_ms_per_tick = 1000.0 / (double)f.QuadPart;
	}
	if (!g_frame_evt)
		g_frame_evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);

	const DWORD pid = GetCurrentProcessId();
	HWND target = nullptr, before = nullptr;
	bool focus_attempted = false, focus_changed = false, focus_restored = false;
	const char* focus_method = "";
	bool down_sent = false, down_observed = false, up_sent = false;
	int up_attempts = 0;
	DWORD win_error = 0;
	uint32_t frame_down = 0, frame_up = 0;
	LONG frames_held = 0;
	LONG creates_before = 0;     // what XXMI created on its own during the frames before the press
	double hold_ms = 0, baseline_ms = 0, longest_ms = 0, stall_ms = 0;
	const char* released_on = "";
	// How sure it is that XXMI looked at the keyboard while the key was down.
	const char* poll_window = "none";

	do {
		if (!g_frame_evt) { fail("internal_error", "Could not create the frame event."); break; }
		if (!g_swap) { fail("no_swap_chain", "The probe has not found the game's swap chain yet, so it cannot follow frames."); break; }
		if (!g_xxmi.base) { fail("xxmi_not_loaded", std::string("XXMI is not loaded in this process; ") + key.nothing + "."); break; }
		if (rq.key == GameKey::ToggleMods) {
			if (!toggle) { fail("binding_mismatch", toggle_why); break; }
		} else if (!xxmi_reloads_on_f10()) {
			fail("binding_mismatch", "d3dx.ini does not bind both reload_config and reload_fixes to 'no_modifiers VK_F10'. No key was sent.");
			break;
		}
		target = game_window();
		if (!target) { fail("no_game_window", "No visible top-level window belongs to the game process."); break; }
		if (IsIconic(target)) { fail("window_minimized", "The game window is minimized; restore it first. No key was sent."); break; }
		const std::string release_first = std::string("Release held keys before ") + key.before + ": ";
		std::string held = held_keys(&key);
		if (!held.empty()) { fail("keys_held", release_first + held + ". No key was sent."); break; }

		before = GetForegroundWindow();
		if (window_pid(before) != pid) {
			if (!rq.focus) {
				fail("not_foreground", "The game window is not in the foreground and focus=false forbids activating it. No key was sent.");
				break;
			}
			focus_attempted = true;
			const char* how = bring_to_front(target);
			if (!how) {
				fail("focus_refused", "Windows did not let the game take the foreground; click the game window and retry. No key was sent.");
				break;
			}
			focus_method = how;
			focus_changed = true;
		}

		// Three presented frames in the foreground show that the game is rendering.
		InterlockedExchange(&g_presents, 0);
		ResetEvent(g_frame_evt);
		InterlockedExchange(&g_armed, 1);
		const LONG creates_at_arm = g_hstats.xxmi_creates;
		if (!wait_present(0, 1500)) { fail("no_frames", "The game presented no frame while in the foreground. No key was sent."); break; }
		LONGLONG t_first = g_present_qpc;
		if (!wait_present(2, 1500)) { fail("no_frames", "The game stopped presenting frames. No key was sent."); break; }
		baseline_ms = ms_between(t_first, g_present_qpc) / (double)(g_presents - 1);
		stall_ms = k_poll_to_present_ms;

		// Last look immediately before the key goes down.
		if (!foreground_is_ours()) { fail("foreground_lost", "The game lost the foreground before the key was sent. No key was sent."); break; }
		held = held_keys(&key);
		if (!held.empty()) { fail("keys_held", release_first + held + ". No key was sent."); break; }

		creates_before = g_hstats.xxmi_creates - creates_at_arm;
		LONGLONG t_down = ticks();
		down_sent = send_key(key.vk, false, &win_error) == 1;
		if (!down_sent) {
			fail("keydown_failed", "Windows did not accept the " + name + " key-down.");
		} else {
			// SendInput returns only after every low-level keyboard hook on the
			// system has handled the event, which can take longer than a frame.
			// Frames count from the moment the key reads as down, not from the call.
			for (int i = 0; i < 300 && !down_observed; i++) {
				down_observed = (GetAsyncKeyState(key.vk) & 0x8000) != 0;
				if (!down_observed)
					Sleep(1);
			}
		}
		if (down_observed) {
			ResetEvent(g_frame_evt);
			const LONG count_down = g_presents;
			t_down = ticks();
			frame_down = (uint32_t)g_frame_no;
			// XXMI polls right before each present. A present that follows this
			// point by less than k_poll_to_present_ms may have been polled a moment
			// too early; a later one cannot, and neither can the second present.
			// Letting go at that present keeps XXMI from polling the key again and
			// reloading a second time. (A [Key...] binding such as the mods toggle
			// fires once per press however long the key is down.)
			LONGLONG edge = t_down;
			LONG seen = 0;
			for (;;) {
				WaitForSingleObject(g_frame_evt, 4);
				const LONGLONG now = ticks();
				if (!foreground_is_ours()) {
					fail("foreground_lost", "Another window took the foreground while " + name + " was held; the key was released at once.");
					released_on = "foreground_lost";
					break;
				}
				held = held_keys(nullptr);
				if (!held.empty()) {
					fail("modifier_pressed", held + " was pressed while " + name + " was held; the key was released at once.");
					released_on = "modifier_pressed";
					break;
				}
				const LONG frames = g_presents - count_down;
				if (frames != seen) {
					const LONGLONG at = g_present_qpc;
					const double gap = ms_between(edge, at);
					if (gap > longest_ms) longest_ms = gap;
					edge = at;
					seen = frames;
					if (gap >= stall_ms) { released_on = "stalled_frame"; poll_window = "stalled_frame"; break; }
					if (frames >= 2) { released_on = "two_frames"; poll_window = "full_frame"; break; }
				}
				if (ms_between(t_down, now) >= rq.max_hold_ms) {
					// No present for this long: either XXMI is still busy with the key
					// or the game is stuck for another reason. Only the capture can tell.
					released_on = "timeout";
					break;
				}
			}
			frames_held = g_presents - count_down;
			if (frames_held >= 2)
				poll_window = "full_frame";
		}

		// Always let go of the key, whatever happened above.
		for (; up_attempts < 2 && !up_sent; ) {
			up_attempts++;
			DWORD err = 0;
			up_sent = send_key(key.vk, true, &err) == 1;
			if (!up_sent)
				win_error = err;
		}
		hold_ms = ms_between(t_down, ticks());
		frame_up = (uint32_t)g_frame_no;
		if (!up_sent)
			fail("keyup_failed", "Windows did not accept the " + name + " key-up; press and release " + name + " by hand before retrying.");
		else if (down_sent && !down_observed)
			fail("keydown_not_observed", name + " was sent from inside the game but its pressed state never became visible; something filters injected input.");
	} while (0);
	InterlockedExchange(&g_armed, 0);

	if (focus_changed && rq.restore_focus && before && IsWindow(before) && foreground_is_ours()) {
		SetForegroundWindow(before);
		for (int i = 0; i < 40 && !focus_restored; i++) {
			focus_restored = GetForegroundWindow() == before;
			if (!focus_restored)
				Sleep(5);
		}
	}

	w.ks("sender", "probe");
	w.ks("key", key.name);
	w.ks("action", key.action);
	w.key("window"); w.ptr(target);
	w.key("foreground_before"); w.ptr(before);
	w.ku("foreground_before_pid", window_pid(before));
	w.kb("focus_attempted", focus_attempted);
	w.kb("focus_changed", focus_changed);
	w.ks("focus_method", focus_method);
	w.kb("focus_restored", focus_restored);
	w.kb("key_down_sent", down_sent);
	w.kb("key_down_observed", down_observed);
	w.kb("key_up_sent", up_sent);
	w.ku("key_up_attempts", (uint32_t)up_attempts);
	w.ku("frame_at_key_down", frame_down);
	w.ku("frame_at_key_up", frame_up);
	w.ku("frames_while_held", (uint32_t)frames_held);
	w.ks("released_on", released_on);
	w.ks("poll_window", poll_window);
	w.kd("hold_ms", hold_ms);
	w.kd("baseline_frame_ms", baseline_ms);
	w.kd("longest_frame_ms", longest_ms);
	w.kd("stall_threshold_ms", stall_ms);
	w.ku("xxmi_creates_before_press", (uint32_t)creates_before);
	w.ku("xxmi_resource_creates", (uint32_t)g_hstats.xxmi_creates);
	w.ku("xxmi_shader_creates", (uint32_t)g_hstats.xxmi_shader_creates);
	if (win_error)
		w.ku("win32_error", win_error);

	plog("%s: %s %s; focus=%s%s, sent=%d observed=%d up=%d, frames %u..%u (%ld held, %s), hold %.1f ms, frame %.1f ms, longest %.1f ms",
	     key.action, key.name, code, focus_changed ? focus_method : "already", focus_restored ? "+restored" : "",
	     (int)down_sent, (int)down_observed, (int)up_sent, frame_down, frame_up, frames_held,
	     *released_on ? released_on : "-", hold_ms, baseline_ms, longest_ms);
	return code;
}
