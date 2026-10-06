// Interception by vtable-pointer swap on individual COM objects.
//
// Only heap objects that belong to the system D3D11/DXGI runtime are touched:
// the object's vtable pointer is replaced by a private copy with a few entries
// redirected. No code is patched and nothing inside XXMI's module is modified.
#pragma once
#include "common.h"

struct HookedObj {
	void*  obj   = nullptr;   // interface pointer whose vptr was swapped
	void** orig  = nullptr;   // the runtime's own vtable
	void** clone = nullptr;   // our copy
	size_t n     = 0;         // entries copied
	int    role  = 0;
	bool   context1 = false; // capability of this context, not the last one created
};

enum { ROLE_DEVICE = 1, ROLE_CONTEXT = 2, ROLE_SWAPCHAIN = 3 };

extern HookedObj* g_dev;      // real ID3D11Device
extern HookedObj* g_ctx;      // real immediate ID3D11DeviceContext
extern HookedObj* g_swap;     // real IDXGISwapChain
extern bool g_ctx_has_ctx1;   // immediate context implements ID3D11DeviceContext1
extern ModuleRange g_xxmi;    // XXMI's module (base == 0 when not present)
extern D3D_FEATURE_LEVEL g_feature_level;

// Calls into the runtime that bypass our own redirections.
#define OCTX(m)  (((ID3D11DeviceContextVtbl*)g_ctx->orig)->m)
#define OCTX1(m) (((ID3D11DeviceContext1Vtbl*)g_ctx->orig)->m)
#define ODEV(m)  (((ID3D11DeviceVtbl*)g_dev->orig)->m)
static inline ID3D11DeviceContext*  rctx()  { return (ID3D11DeviceContext*)g_ctx->obj; }
static inline ID3D11DeviceContext1* rctx1() { return (ID3D11DeviceContext1*)g_ctx->obj; }
static inline ID3D11Device*         rdev()  { return (ID3D11Device*)g_dev->obj; }

bool hooks_install_device(ID3D11Device* real_device);
bool hooks_install_context(ID3D11DeviceContext* real_context);
// Hook every device, but only a swap chain may replace the provisional target.
bool hooks_attach_device(ID3D11Device* real_device, ID3D11DeviceContext* real_context);
void hooks_find_xxmi();

// Verifies that the swapped vtable pointers are still in place (the runtime may
// replace them) and re-applies them when needed. Called once per frame.
void hooks_verify();

// Stack-depth heuristic: how many consecutive callers belong to XXMI's module.
// A direct forward of a game call is shallow, XXMI's own work is deep.
struct CallerInfo {
	uint8_t  origin = 0;      // Origin
	uint16_t depth = 0;       // consecutive frames inside XXMI
	uint32_t rva = 0;         // return address relative to XXMI's base (0 if caller is not XXMI)
};
void classify_caller(void* const* frames, unsigned count, CallerInfo* out);

struct HookStats {
	volatile LONG draws, creates, presents, maps, exec_cmdlists, revptr;
	volatile LONG tracked_draws;
};
extern HookStats g_hstats;
