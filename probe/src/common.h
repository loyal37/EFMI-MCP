// EFMI probe - shared declarations.
//
// The probe is a passive observer that lives next to XXMI (3DMigoto) inside the
// game process. It never touches XXMI's files or its module image: everything
// is done by swapping vtable pointers on the *system* D3D11/DXGI objects that
// XXMI itself calls into.
#pragma once

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define CINTERFACE
#define COBJMACROS
#define D3D11_NO_HELPERS
#include <windows.h>
#include <d3d11_4.h>
#include <dxgi1_4.h>

#include <stdint.h>
#include <stddef.h>
#include <string>
#include <vector>

#define PROBE_VERSION "0.1.1"
#define PROBE_PROTOCOL 1

// ---------------------------------------------------------------- logging
void log_init(HMODULE self);
void plog(const char* fmt, ...);
const wchar_t* probe_dir();          // directory the probe DLL was loaded from

// ---------------------------------------------------------------- settings
struct Settings {
	bool     keep_bytecode   = true;    // keep a copy of every shader's bytecode
	uint32_t bytecode_cap_mb = 256;
	uint32_t arena_chunk_mb  = 8;       // staging chunk used for draw-time readback
	uint32_t arena_cap_mb    = 768;
	uint32_t max_draws       = 60000;   // per captured frame
	bool     stack_origin    = true;    // classify game vs XXMI calls by stack depth

	// Mirrors of the XXMI settings that influence hashes (read from d3dx.ini).
	int      texture_hash    = 0;       // [Rendering] texture_hash
	bool     res_from_swap_chain = true;// [Device] get_resolution_from = swap_chain
	bool     track_region_hashes = true;
	int      shader_hash     = 0;       // 0 = 3dmigoto (FNV), 1 = embedded, 2 = bytecode
};
extern Settings g_cfg;
void settings_load();                  // probe ini (next to the DLL)
void settings_load_xxmi(const wchar_t* d3dx_ini_path);

// ---------------------------------------------------------------- misc
struct ModuleRange { uintptr_t base = 0; uintptr_t end = 0; wchar_t path[MAX_PATH] = {}; };
bool module_from_address(const void* addr, ModuleRange* out);
bool ptr_in(const ModuleRange& m, const void* p);
bool is_exec_ptr(const void* p);
bool is_system_module_path(const wchar_t* path);
std::string narrow(const wchar_t* w);
uint64_t now_ms();

// Global frame counter (incremented on every real Present).
extern volatile LONG g_frame_no;

// ---------------------------------------------------------------- lifecycle
void bootstrap_start();                 // install the device-creation hook
void probe_attach(IUnknown* device, IUnknown* context, const char* how);
void ipc_start();
