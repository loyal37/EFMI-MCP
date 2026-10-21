// Recording: everything drawn between the user switching it on and off (Ctrl+F9 in the game by
// default, or the client), written into a folder of its own: per mesh/shader line the stretches of
// frames it was drawn in, per frame the number of draws, and now and then a frame in full (a
// "keyframe"). What is recorded is decided in capture.cpp; the files are written here, on a thread
// of their own.
#pragma once
#include "common.h"
#include <memory>

struct Capture;

struct RecordConfig {
	bool     hotkey = true;              // the key below switches recording on and off
	uint32_t key = VK_F9;                // ... while a window of the game has the foreground
	bool     ctrl = true, shift = false, alt = false;
	bool     beep = true;                // a sound when a recording starts and when it stops
	std::wstring root;                   // every recording gets a folder of its own in here
	uint32_t keyframe_every_s = 10;      // a full frame at least this often (0: only at the start and on news)
	uint32_t keyframe_gap_ms = 2000;     // at least this long between keyframes started by something new
	bool     keyframe_cb = true;         // keyframes keep the constant buffers as each draw saw them
	uint32_t max_keyframes = 300;
	uint32_t disk_mb = 4096;             // no more keyframes once a recording's folder holds this much
	uint32_t max_minutes = 60;           // a recording stops by itself after this long (0: never)
};

void rec_init();                         // after settings_load(): reads [Record] of efmi_probe.ini
const RecordConfig& rec_config();
std::string rec_hotkey_name();           // e.g. "ctrl+F9"

// ---- render thread: a recording's data, in the order it is to be written
uint32_t rec_open(const char* how, uint32_t frame, int res_w, int res_h);   // returns the recording's number
void rec_lines(uint32_t id, std::string&& jsonl);        // lines seen for the first time
void rec_runs(uint32_t id, std::string&& jsonl);         // finished runs
void rec_frames(uint32_t id, std::vector<uint8_t>&& records);
void rec_open_runs(uint32_t id, std::string&& json);     // runs not finished yet, kept in case the game ends
void rec_keyframe(uint32_t id, std::shared_ptr<Capture> c, const char* reason, const std::string& what,
                  uint32_t t_ms);
void rec_close(uint32_t id, std::string&& members);      // members appended to recording.json
uint64_t rec_bytes(uint32_t id);                         // written so far into that recording's folder

// ---- any thread
void rec_status_members(std::string* out);               // "folder", "error", "bytes", "pending" as JSON members
bool rec_flush(uint32_t timeout_ms);                     // waits until everything handed over is written
