// Presses one of two fixed keys from inside the game process: XXMI's reload key
// (F10) or EFMI's key that switches all mods off and on (F11).
//
// XXMI polls GetAsyncKeyState once per presented frame, and only while a window
// of this process has the foreground. The game runs elevated, so a key sent by
// the unelevated MCP server never arrives: Windows drops input that a lower
// integrity level sends to a higher one, and reports success anyway. Sent from
// here it is an ordinary key press. Nothing in XXMI or in the game is patched,
// and the client chooses between the two functions, never a key.
#pragma once
#include "common.h"
#include <string>

class JsonW;

enum class GameKey {
	Reload,       // F10: [Hunting] reload_config / reload_fixes in d3dx.ini
	ToggleMods,   // F11: [KeyToggleMods] in Core\EFMI\KeyBindings.ini
};

struct KeyPressRequest {
	GameKey  key = GameKey::Reload;
	bool     focus = true;          // bring the game window to the foreground when it is not there
	bool     restore_focus = true;  // afterwards hand the foreground back to the window that had it
	uint32_t max_hold_ms = 3000;    // upper bound for keeping the key down
};

// IPC thread. Writes the members of the receipt into the object that is open in
// `w` and returns the outcome code ("ok" when the key was pressed and released
// as intended); `message` explains any other code.
const char* key_press_run(const KeyPressRequest& rq, JsonW& w, std::string* message);

// Render thread, once per presented frame. Idle unless a key press is in progress.
void reload_on_present();

// The window the selected swap chain presents to.
void reload_set_output_window(HWND hwnd);

// Elevation of this process, for the status report.
void reload_process_info(bool* elevated, uint32_t* integrity_rid);
