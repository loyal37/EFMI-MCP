// Slot cache: while the game runs, the probe notes for every mesh it has not seen yet which shaders
// draw it and which resources sit in which shader slots, and appends that to files kept per game
// version next to the DLL. Switching characters in the game is enough to fill it; nothing has to be
// requested. The files are what a later version is compared with when texture slots move.
#pragma once
#include "common.h"

struct SlotCacheConfig {
	bool        enable = true;
	uint32_t    every = 30;           // look at one frame in this many
	bool        skinned_only = true;  // only draws whose input layout has BLENDINDICES
	uint32_t    max_new = 256;        // at most this many new draws are recorded in one looked-at frame
	std::string subdir = "slot-cache";// below the probe's directory; a plain name
};

void slot_init();                                   // after settings_load(): reads [SlotCache] of efmi_probe.ini
void slot_get_config(SlotCacheConfig* out);
// Returns false (and leaves everything as it was) when subdir is not a plain name.
bool slot_set_config(const SlotCacheConfig& cfg);

// ---- render thread
bool slot_due(uint32_t frame);                      // is this frame to be looked at
bool slot_skinned_only();
uint32_t slot_max_new();
LONG slot_generation();                             // changes when the files change folder: forget what was seen

// One finished record (a JSON object) per new draw, with the key it is told apart by and the
// shaders whose bytecode belongs with it.
void slot_submit(std::string&& record, std::string&& key, uint64_t vs, uint64_t ps);
void slot_note_frame(double record_ms, uint32_t new_draws, uint32_t deferred);

// ---- any thread
// Waits until everything submitted so far is on disk.
bool slot_flush(uint32_t timeout_ms);
void slot_status_object(std::string* out);          // a JSON object: settings, folder, counters
