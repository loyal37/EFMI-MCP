#include "reload.h"
#include "hooks.h"
#include "json.h"
#include <string.h>
#include <wctype.h>

// XXMI reads the keyboard at the start of its Present wrapper and then calls
// the real Present, where this probe sees the frame. In between it only draws
// its overlay, far quicker than this; a reload takes many times longer.
static const double     k_poll_to_present_ms = 10.0;

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

static std::string held_keys(bool include_f10)
{
	static const struct { int vk; const char* name; } keys[] = {
		{ VK_SHIFT, "Shift" }, { VK_CONTROL, "Ctrl" }, { VK_MENU, "Alt" },
		{ VK_LWIN, "Left Win" }, { VK_RWIN, "Right Win" }, { VK_F10, "F10" },
	};
	std::string out;
	for (const auto& k : keys) {
		if (k.vk == VK_F10 && !include_f10)
			continue;
		if (GetAsyncKeyState(k.vk) & 0x8000) {
			if (!out.empty()) out += ", ";
			out += k.name;
		}
	}
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

// "no_modifiers VK_F10", ignoring case, spacing and a trailing comment.
static bool binding_is_plain_f10(const wchar_t* ini, const wchar_t* key)
{
	wchar_t buf[128];
	GetPrivateProfileStringW(L"Hunting", key, L"", buf, _countof(buf), ini);
	std::wstring norm;
	bool space = false;
	for (const wchar_t* p = buf; *p && *p != L';'; p++) {
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
	return norm == L"no_modifiers vk_f10";
}

static bool xxmi_reloads_on_f10()
{
	wchar_t ini[MAX_PATH];
	wcscpy_s(ini, g_xxmi.path);
	wchar_t* slash = wcsrchr(ini, L'\\');
	if (!slash)
		return false;
	slash[1] = 0;
	if (wcscat_s(ini, L"d3dx.ini"))
		return false;
	return binding_is_plain_f10(ini, L"reload_config") && binding_is_plain_f10(ini, L"reload_fixes");
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

static UINT send_f10(bool up, DWORD* error)
{
	INPUT in = {};
	in.type = INPUT_KEYBOARD;
	in.ki.wVk = VK_F10;
	in.ki.wScan = (WORD)MapVirtualKeyW(VK_F10, MAPVK_VK_TO_VSC);
	in.ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;
	SetLastError(0);
	UINT n = SendInput(1, &in, sizeof(in));
	*error = GetLastError();
	return n;
}

// ---------------------------------------------------------------- the press
const char* reload_key_run(const ReloadKeyRequest& rq, JsonW& w, std::string* message)
{
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
		if (!g_xxmi.base) { fail("xxmi_not_loaded", "XXMI is not loaded in this process; there is nothing to reload."); break; }
		if (!xxmi_reloads_on_f10()) {
			fail("binding_mismatch", "d3dx.ini does not bind both reload_config and reload_fixes to 'no_modifiers VK_F10'. No key was sent.");
			break;
		}
		target = game_window();
		if (!target) { fail("no_game_window", "No visible top-level window belongs to the game process."); break; }
		if (IsIconic(target)) { fail("window_minimized", "The game window is minimized; restore it first. No key was sent."); break; }
		std::string held = held_keys(true);
		if (!held.empty()) { fail("keys_held", "Release held keys before the reload: " + held + ". No key was sent."); break; }

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
		held = held_keys(true);
		if (!held.empty()) { fail("keys_held", "Release held keys before the reload: " + held + ". No key was sent."); break; }

		creates_before = g_hstats.xxmi_creates - creates_at_arm;
		LONGLONG t_down = ticks();
		down_sent = send_f10(false, &win_error) == 1;
		if (!down_sent) {
			fail("keydown_failed", "Windows did not accept the F10 key-down.");
		} else {
			// SendInput returns only after every low-level keyboard hook on the
			// system has handled the event, which can take longer than a frame.
			// Frames count from the moment the key reads as down, not from the call.
			for (int i = 0; i < 300 && !down_observed; i++) {
				down_observed = (GetAsyncKeyState(VK_F10) & 0x8000) != 0;
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
			// reloading a second time.
			LONGLONG edge = t_down;
			LONG seen = 0;
			for (;;) {
				WaitForSingleObject(g_frame_evt, 4);
				const LONGLONG now = ticks();
				if (!foreground_is_ours()) {
					fail("foreground_lost", "Another window took the foreground while F10 was held; the key was released at once.");
					released_on = "foreground_lost";
					break;
				}
				held = held_keys(false);
				if (!held.empty()) {
					fail("modifier_pressed", held + " was pressed while F10 was held; the key was released at once.");
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
					// No present for this long: either XXMI is still reloading or the
					// game is stuck for another reason. Only the capture can tell.
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
			up_sent = send_f10(true, &err) == 1;
			if (!up_sent)
				win_error = err;
		}
		hold_ms = ms_between(t_down, ticks());
		frame_up = (uint32_t)g_frame_no;
		if (!up_sent)
			fail("keyup_failed", "Windows did not accept the F10 key-up; press and release F10 by hand before retrying.");
		else if (down_sent && !down_observed)
			fail("keydown_not_observed", "F10 was sent from inside the game but its pressed state never became visible; something filters injected input.");
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
	w.ks("key", "F10");
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

	plog("reload: F10 %s; focus=%s%s, sent=%d observed=%d up=%d, frames %u..%u (%ld held, %s), hold %.1f ms, frame %.1f ms, longest %.1f ms",
	     code, focus_changed ? focus_method : "already", focus_restored ? "+restored" : "",
	     (int)down_sent, (int)down_observed, (int)up_sent, frame_down, frame_up, frames_held,
	     *released_on ? released_on : "-", hold_ms, baseline_ms, longest_ms);
	return code;
}
