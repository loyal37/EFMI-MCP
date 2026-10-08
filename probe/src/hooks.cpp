#include "hooks.h"
#include "capture.h"
#include "registry.h"
#include "hash.h"
#include "reload.h"
#include <psapi.h>
#include <string.h>

HookedObj* g_dev = nullptr;
HookedObj* g_ctx = nullptr;
HookedObj* g_swap = nullptr;
bool g_ctx_has_ctx1 = false;
ModuleRange g_xxmi;
D3D_FEATURE_LEVEL g_feature_level = (D3D_FEATURE_LEVEL)0;
HookStats g_hstats;

static HookedObj g_objs[32];
static volatile LONG g_nobjs = 0;
static volatile LONG g_in_present = 0;
static SRWLOCK g_target_lock = SRWLOCK_INIT;
static bool g_target_bound = false; // device is pinned once a swap chain owns it
static SRWLOCK g_discovery_lock = SRWLOCK_INIT;
static void* g_checked[16];
static unsigned g_checked_pos = 0;

// Forwarded game calls pass through at most this many XXMI frames.
static const unsigned k_origin_max_depth = 2;

// ================================================================ plumbing
static bool read_ptr(void* const* addr, void** out)
{
	__try {
		*out = *addr;
		return true;
	} __except (EXCEPTION_EXECUTE_HANDLER) {
		return false;
	}
}

static HookedObj* find_obj(void* obj)
{
	LONG n = g_nobjs;
	if (n > (LONG)_countof(g_objs)) n = (LONG)_countof(g_objs);
	for (LONG i = 0; i < n; i++)
		if (g_objs[i].obj == obj)
			return &g_objs[i];
	return nullptr;
}

static bool clone_into(HookedObj* h, void* obj, size_t min_entries)
{
	void** vt = nullptr;
	if (!read_ptr((void* const*)obj, (void**)&vt) || !vt)
		return false;

	// Copy as many entries as look like code. System vtables are laid out back
	// to back, so this over-copies, which is harmless and keeps newer interface
	// revisions working when somebody queries for them.
	size_t n = 0;
	void* p = nullptr;
	while (n < 1024 && read_ptr(&vt[n], &p) && is_exec_ptr(p))
		n++;
	if (n < min_entries) {
		plog("hooks: vtable at %p has only %zu entries (need %zu), not hooking", vt, n, min_entries);
		return false;
	}
	void** mem = (void**)HeapAlloc(GetProcessHeap(), 0, (n + 1) * sizeof(void*));
	if (!mem)
		return false;
	void* before = nullptr;
	read_ptr(&vt[-1], &before);        // RTTI locator slot, if the module has one
	mem[0] = before;
	memcpy(mem + 1, vt, n * sizeof(void*));
	h->obj = obj;
	h->orig = vt;
	h->clone = mem + 1;
	h->n = n;
	return true;
}

static SRWLOCK g_objs_lock = SRWLOCK_INIT;

static HookedObj* new_obj(void* obj, size_t min_entries, int role)
{
	HookedObj* h = nullptr;
	AcquireSRWLockExclusive(&g_objs_lock);
	HookedObj* existing = find_obj(obj);
	bool still_hooked = false;
	if (existing) {
		// Only "already hooked" if the object still carries our table. A new
		// object can be created at the address of one that was destroyed.
		void* cur = nullptr;
		still_hooked = read_ptr((void* const*)obj, &cur) && cur == (void*)existing->clone;
		if (!still_hooked)
			existing->obj = nullptr;
	}
	if (still_hooked) {
		ReleaseSRWLockExclusive(&g_objs_lock);
		return existing->role == role ? existing : nullptr;
	}
	if (!still_hooked) {
		LONG n = g_nobjs;
		// Reuse the slot of an object that has since been destroyed.
		for (LONG i = 0; i < n && !h; i++)
			if (!g_objs[i].obj && g_objs[i].clone)
				h = &g_objs[i];
		bool fresh_slot = false;
		if (!h && n < (LONG)_countof(g_objs)) {
			h = &g_objs[n];
			fresh_slot = true;
		}
		if (!h) {
			plog("hooks: object table is full");
		} else {
			HookedObj tmp;
			tmp.role = role;
			if (clone_into(&tmp, obj, min_entries)) {
				if (h->clone)
					HeapFree(GetProcessHeap(), 0, h->clone - 1);
				// Publish `obj` last: lookups match on it.
				h->orig = tmp.orig;
				h->clone = tmp.clone;
				h->n = tmp.n;
				h->role = tmp.role;
				h->context1 = false;
				h->obj = tmp.obj;
				if (fresh_slot)
					InterlockedIncrement(&g_nobjs);
			} else {
				h = nullptr;
			}
		}
	}
	ReleaseSRWLockExclusive(&g_objs_lock);
	return h;
}

static void activate(HookedObj* h)
{
	InterlockedExchangePointer((void* volatile*)h->obj, h->clone);
}

void classify_caller(void* const* frames, unsigned count, CallerInfo* out)
{
	out->origin = ORIGIN_UNKNOWN;
	out->depth = 0;
	out->rva = 0;
	if (!g_xxmi.base) {
		out->origin = ORIGIN_GAME;     // nothing sits between the game and D3D
		return;
	}
	unsigned d = 0;
	while (d < count && ptr_in(g_xxmi, frames[d]))
		d++;
	out->depth = (uint16_t)d;
	if (d == 0) {
		out->origin = ORIGIN_GAME;     // caller reached the device without XXMI
		return;
	}
	out->rva = (uint32_t)((uintptr_t)frames[0] - g_xxmi.base);
	out->origin = (d <= k_origin_max_depth) ? ORIGIN_GAME : ORIGIN_XXMI;
}

#define CALLER(ci) \
	CallerInfo ci; \
	if (g_cfg.stack_origin) { \
		void* fr_[10]; \
		USHORT n_ = RtlCaptureStackBackTrace(1, 10, fr_, nullptr); \
		classify_caller(fr_, n_, &ci); \
	}

void hooks_find_xxmi()
{
	if (g_xxmi.base)
		return;
	HMODULE mods[1024];
	DWORD needed = 0;
	if (!K32EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &needed))
		return;
	DWORD count = needed / sizeof(HMODULE);
	if (count > _countof(mods)) count = _countof(mods);
	for (DWORD i = 0; i < count; i++) {
		wchar_t path[MAX_PATH];
		if (!GetModuleFileNameW(mods[i], path, MAX_PATH))
			continue;
		const wchar_t* base = wcsrchr(path, L'\\');
		base = base ? base + 1 : path;
		if (_wcsicmp(base, L"d3d11.dll") || is_system_module_path(path))
			continue;
		// A d3d11.dll outside the Windows directory is the wrapper (XXMI).
		if (!module_from_address(mods[i], &g_xxmi))
			continue;
		plog("hooks: XXMI module %p..%p %s", (void*)g_xxmi.base, (void*)g_xxmi.end, narrow(path).c_str());
		wchar_t ini[MAX_PATH];
		wcscpy_s(ini, path);
		wchar_t* slash = wcsrchr(ini, L'\\');
		if (slash) {
			slash[1] = 0;
			wcscat_s(ini, L"d3dx.ini");
			settings_load_xxmi(ini);
		}
		return;
	}
}

// ================================================================ swap chain
static HRESULT STDMETHODCALLTYPE H_Present(IDXGISwapChain* This, UINT SyncInterval, UINT Flags)
{
	HookedObj* h = find_obj(This);
	if (!(Flags & DXGI_PRESENT_TEST) && h == g_swap && InterlockedIncrement(&g_in_present) == 1) {
		g_hstats.presents++;
		cap_on_frame_boundary();
		reload_on_present();
	}
	HRESULT hr = ((IDXGISwapChainVtbl*)h->orig)->Present(This, SyncInterval, Flags);
	if (!(Flags & DXGI_PRESENT_TEST) && h == g_swap)
		InterlockedDecrement(&g_in_present);
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_Present1(IDXGISwapChain1* This, UINT SyncInterval, UINT Flags,
                                            const DXGI_PRESENT_PARAMETERS* pParams)
{
	HookedObj* h = find_obj(This);
	if (!(Flags & DXGI_PRESENT_TEST) && h == g_swap && InterlockedIncrement(&g_in_present) == 1) {
		g_hstats.presents++;
		cap_on_frame_boundary();
		reload_on_present();
	}
	HRESULT hr = ((IDXGISwapChain1Vtbl*)h->orig)->Present1(This, SyncInterval, Flags, pParams);
	if (!(Flags & DXGI_PRESENT_TEST) && h == g_swap)
		InterlockedDecrement(&g_in_present);
	return hr;
}

static void update_resolution(IDXGISwapChain* sc, const char* why)
{
	DXGI_SWAP_CHAIN_DESC d;
	memset(&d, 0, sizeof(d));
	HookedObj* h = find_obj(sc);
	IDXGISwapChainVtbl* vt = h ? (IDXGISwapChainVtbl*)h->orig : (IDXGISwapChainVtbl*)sc->lpVtbl;
	if (SUCCEEDED(vt->GetDesc(sc, &d)) && d.BufferDesc.Width && d.BufferDesc.Height) {
		hash_set_resolution((int)d.BufferDesc.Width, (int)d.BufferDesc.Height);
		reload_set_output_window(d.OutputWindow);
		plog("hooks: resolution %ux%u (%s), back buffer format %u, buffers %u, swap effect %u",
		     d.BufferDesc.Width, d.BufferDesc.Height, why, (unsigned)d.BufferDesc.Format,
		     d.BufferCount, (unsigned)d.SwapEffect);
	}
}

static HRESULT STDMETHODCALLTYPE H_ResizeBuffers(IDXGISwapChain* This, UINT BufferCount, UINT Width, UINT Height,
                                                 DXGI_FORMAT NewFormat, UINT Flags)
{
	HookedObj* h = find_obj(This);
	HRESULT hr = ((IDXGISwapChainVtbl*)h->orig)->ResizeBuffers(This, BufferCount, Width, Height, NewFormat, Flags);
	if (SUCCEEDED(hr) && h == g_swap)
		update_resolution(This, "ResizeBuffers");
	return hr;
}

static ULONG STDMETHODCALLTYPE H_SwapRelease(IDXGISwapChain* This)
{
	HookedObj* h = find_obj(This);
	ULONG r = ((IDXGISwapChainVtbl*)h->orig)->Release(This);
	if (r == 0) {
		// The object is gone; forget it so a new swap chain can be picked up.
		if (g_swap == h) {
			AcquireSRWLockExclusive(&g_target_lock);
			g_swap = nullptr;
			// The replacement may reuse the same back-buffer address.
			AcquireSRWLockExclusive(&g_discovery_lock);
			memset(g_checked, 0, sizeof(g_checked));
			g_checked_pos = 0;
			ReleaseSRWLockExclusive(&g_discovery_lock);
			ReleaseSRWLockExclusive(&g_target_lock);
			plog("hooks: swap chain %p released", This);
		}
		h->obj = nullptr;
	}
	return r;
}

static void hook_swapchain(IDXGISwapChain* sc)
{
	// The back buffer's owner decides which immediate context is recorded.
	// Device creation order is unrelated to which device draws the game.
	ID3D11Device* dev = nullptr;
	ID3D11DeviceContext* ctx = nullptr;
	if (FAILED(IDXGISwapChain_GetDevice(sc, IID_ID3D11Device, (void**)&dev)) || !dev)
		return;
	ID3D11Device_GetImmediateContext(dev, &ctx);
	if (!ctx || !hooks_attach_device(dev, ctx)) {
		if (ctx) ID3D11DeviceContext_Release(ctx);
		ID3D11Device_Release(dev);
		return;
	}
	HookedObj* dh = find_obj(dev);
	HookedObj* ch = find_obj(ctx);
	AcquireSRWLockExclusive(&g_target_lock);
	if (g_swap || (g_target_bound && (g_dev != dh || g_ctx != ch))) {
		// Readback pools and pending captures belong to the pinned device.
		// Do not silently issue their commands on a replacement device.
		if (!g_swap)
			plog("hooks: swap chain belongs to a replacement device; restart the game to reset the probe safely");
		ReleaseSRWLockExclusive(&g_target_lock);
		ID3D11DeviceContext_Release(ctx);
		ID3D11Device_Release(dev);
		return;
	}
	HookedObj* h = new_obj(sc, sizeof(IDXGISwapChainVtbl) / sizeof(void*), ROLE_SWAPCHAIN);
	if (!h) {
		ReleaseSRWLockExclusive(&g_target_lock);
		ID3D11DeviceContext_Release(ctx);
		ID3D11Device_Release(dev);
		return;
	}
	IDXGISwapChainVtbl* v = (IDXGISwapChainVtbl*)h->clone;
	v->Present = H_Present;
	v->ResizeBuffers = H_ResizeBuffers;
	v->Release = H_SwapRelease;

	IDXGISwapChain1* sc1 = nullptr;
	if (SUCCEEDED(IDXGISwapChain_QueryInterface(sc, IID_IDXGISwapChain1, (void**)&sc1)) && sc1) {
		if ((void*)sc1 == (void*)sc && h->n >= sizeof(IDXGISwapChain1Vtbl) / sizeof(void*))
			((IDXGISwapChain1Vtbl*)h->clone)->Present1 = H_Present1;
		((IDXGISwapChain1Vtbl*)sc1->lpVtbl)->Release(sc1);
	}
	if (g_dev != dh || g_ctx != ch) {
		// Keep the selected objects alive even if their swap chain is released.
		ID3D11Device_AddRef(dev);
		ID3D11DeviceContext_AddRef(ctx);
		if (g_ctx) ID3D11DeviceContext_Release((ID3D11DeviceContext*)g_ctx->obj);
		if (g_dev) ID3D11Device_Release((ID3D11Device*)g_dev->obj);
	}
	g_dev = dh;
	g_ctx = ch;
	g_ctx_has_ctx1 = ch->context1;
	g_feature_level = ((ID3D11DeviceVtbl*)dh->orig)->GetFeatureLevel(dev);
	cap_target_selected();
	g_target_bound = true;
	g_swap = h;
	activate(h);
	ReleaseSRWLockExclusive(&g_target_lock);
	update_resolution(sc, "swap chain found");
	plog("hooks: swap chain %p hooked (%zu vtable entries), capture device %p context %p",
	     sc, h->n, dh->obj, ch->obj);
	ID3D11DeviceContext_Release(ctx);
	ID3D11Device_Release(dev);
}

// Finds the swap chain through one of its back buffers: DXGI can tell which
// swap chain, if any, owns a texture that is being used as a render target.
static void try_discover_swapchain(ID3D11Resource* res)
{
	if (g_swap || !res)
		return;
	AcquireSRWLockExclusive(&g_discovery_lock);
	for (unsigned i = 0; i < _countof(g_checked); i++)
		if (g_checked[i] == res) {
			ReleaseSRWLockExclusive(&g_discovery_lock);
			return;
		}
	g_checked[g_checked_pos++ % _countof(g_checked)] = res;
	ReleaseSRWLockExclusive(&g_discovery_lock);

	// Ask DXGI directly whether a swap chain owns this texture. (The registry
	// is not consulted: an entry left behind at a recycled address would make a
	// back buffer look like an ordinary texture.)
	IDXGISurface* surf = nullptr;
	if (FAILED(ID3D11Resource_QueryInterface(res, IID_IDXGISurface, (void**)&surf)) || !surf)
		return;
	IDXGISwapChain* sc = nullptr;
	HRESULT hr = IDXGISurface_GetParent(surf, IID_IDXGISwapChain, (void**)&sc);
	IDXGISurface_Release(surf);
	if (FAILED(hr) || !sc)
		return;
	hook_swapchain(sc);
	// Drop our reference through the runtime's own table so the release hook
	// does not mistake it for the final one.
	HookedObj* h = find_obj(sc);
	if (h)
		((IDXGISwapChainVtbl*)h->orig)->Release(sc);
	else
		IDXGISwapChain_Release(sc);
}

// ================================================================ device
static inline HookedObj* dev_of(ID3D11Device* This)
{
	if (g_dev && g_dev->obj == This)
		return g_dev;
	return find_obj(This);
}
#define DEV_ORIG(h) ((ID3D11DeviceVtbl*)(h)->orig)

static HRESULT STDMETHODCALLTYPE H_CreateBuffer(ID3D11Device* This, const D3D11_BUFFER_DESC* pDesc,
                                                const D3D11_SUBRESOURCE_DATA* pInitialData, ID3D11Buffer** ppBuffer)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateBuffer(This, pDesc, pInitialData, ppBuffer);
	if (SUCCEEDED(hr) && ppBuffer && *ppBuffer && pDesc) {
		CALLER(ci);
		ResInfo ri;
		ri.dim = D3D11_RESOURCE_DIMENSION_BUFFER;
		ri.origin = ci.origin;
		ri.desc.buf = *pDesc;
		ri.res_known = true;
		ri.hash = hash_buffer(pDesc, pInitialData, &ri.data_hash);
		ri.has_init = pInitialData && pInitialData->pSysMem;
		reg_add_resource(*ppBuffer, ri);
		g_hstats.creates++;
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_creates++;
	}
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_CreateTexture1D(ID3D11Device* This, const D3D11_TEXTURE1D_DESC* pDesc,
                                                   const D3D11_SUBRESOURCE_DATA* pInitialData, ID3D11Texture1D** ppTexture)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateTexture1D(This, pDesc, pInitialData, ppTexture);
	if (SUCCEEDED(hr) && ppTexture && *ppTexture && pDesc) {
		CALLER(ci);
		ResInfo ri;
		ri.dim = D3D11_RESOURCE_DIMENSION_TEXTURE1D;
		ri.origin = ci.origin;
		ri.desc.t1 = *pDesc;
		ri.res_known = true;
		ri.hash = hash_tex1d(pDesc, pInitialData, &ri.data_hash);
		ri.has_init = pInitialData && pInitialData->pSysMem;
		reg_add_resource(*ppTexture, ri);
		g_hstats.creates++;
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_creates++;
	}
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_CreateTexture2D(ID3D11Device* This, const D3D11_TEXTURE2D_DESC* pDesc,
                                                   const D3D11_SUBRESOURCE_DATA* pInitialData, ID3D11Texture2D** ppTexture)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateTexture2D(This, pDesc, pInitialData, ppTexture);
	if (SUCCEEDED(hr) && ppTexture && *ppTexture && pDesc) {
		CALLER(ci);
		int rw, rh;
		hash_get_resolution(&rw, &rh);
		ResInfo ri;
		ri.dim = D3D11_RESOURCE_DIMENSION_TEXTURE2D;
		ri.origin = ci.origin;
		ri.desc.t2 = *pDesc;
		ri.res_known = rw > 0;
		ri.hash = hash_tex2d(pDesc, pInitialData, &ri.data_hash);
		ri.has_init = pInitialData && pInitialData->pSysMem;
		reg_add_resource(*ppTexture, ri);
		g_hstats.creates++;
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_creates++;
	}
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_CreateTexture3D(ID3D11Device* This, const D3D11_TEXTURE3D_DESC* pDesc,
                                                   const D3D11_SUBRESOURCE_DATA* pInitialData, ID3D11Texture3D** ppTexture)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateTexture3D(This, pDesc, pInitialData, ppTexture);
	if (SUCCEEDED(hr) && ppTexture && *ppTexture && pDesc) {
		CALLER(ci);
		int rw, rh;
		hash_get_resolution(&rw, &rh);
		ResInfo ri;
		ri.dim = D3D11_RESOURCE_DIMENSION_TEXTURE3D;
		ri.origin = ci.origin;
		ri.desc.t3 = *pDesc;
		ri.res_known = rw > 0;
		ri.hash = hash_tex3d(pDesc, pInitialData, &ri.data_hash);
		ri.has_init = pInitialData && pInitialData->pSysMem;
		reg_add_resource(*ppTexture, ri);
		g_hstats.creates++;
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_creates++;
	}
	return hr;
}

#define SHADER_HOOK(Name, Iface, tag) \
static HRESULT STDMETHODCALLTYPE H_Create##Name(ID3D11Device* This, const void* pBytecode, SIZE_T Length, \
                                               ID3D11ClassLinkage* pLinkage, Iface** ppShader) \
{ \
	HookedObj* h = dev_of(This); \
	HRESULT hr = DEV_ORIG(h)->Create##Name(This, pBytecode, Length, pLinkage, ppShader); \
	if (SUCCEEDED(hr) && ppShader && *ppShader && pBytecode && Length) { \
		CALLER(ci); \
		reg_add_shader(*ppShader, tag, shader_hash(pBytecode, Length), pBytecode, Length, ci.origin); \
		g_hstats.creates++; \
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_shader_creates++; \
	} \
	return hr; \
}
SHADER_HOOK(VertexShader, ID3D11VertexShader, "vs")
SHADER_HOOK(HullShader, ID3D11HullShader, "hs")
SHADER_HOOK(DomainShader, ID3D11DomainShader, "ds")
SHADER_HOOK(GeometryShader, ID3D11GeometryShader, "gs")
SHADER_HOOK(PixelShader, ID3D11PixelShader, "ps")
SHADER_HOOK(ComputeShader, ID3D11ComputeShader, "cs")

static HRESULT STDMETHODCALLTYPE H_CreateGeometryShaderWithStreamOutput(
	ID3D11Device* This, const void* pBytecode, SIZE_T Length, const D3D11_SO_DECLARATION_ENTRY* pSODeclaration,
	UINT NumEntries, const UINT* pBufferStrides, UINT NumStrides, UINT RasterizedStream,
	ID3D11ClassLinkage* pLinkage, ID3D11GeometryShader** ppShader)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateGeometryShaderWithStreamOutput(This, pBytecode, Length, pSODeclaration, NumEntries,
	                                                              pBufferStrides, NumStrides, RasterizedStream, pLinkage, ppShader);
	if (SUCCEEDED(hr) && ppShader && *ppShader && pBytecode && Length) {
		CALLER(ci);
		reg_add_shader(*ppShader, "gs", shader_hash(pBytecode, Length), pBytecode, Length, ci.origin);
		g_hstats.creates++;
		if (ci.origin == ORIGIN_XXMI) g_hstats.xxmi_shader_creates++;
	}
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_CreateInputLayout(ID3D11Device* This, const D3D11_INPUT_ELEMENT_DESC* pElems,
                                                     UINT NumElements, const void* pBytecode, SIZE_T Length,
                                                     ID3D11InputLayout** ppLayout)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateInputLayout(This, pElems, NumElements, pBytecode, Length, ppLayout);
	if (SUCCEEDED(hr) && ppLayout && *ppLayout)
		reg_add_layout(*ppLayout, pElems, NumElements);
	return hr;
}

static HRESULT STDMETHODCALLTYPE H_CreateRenderTargetView(ID3D11Device* This, ID3D11Resource* pResource,
                                                          const D3D11_RENDER_TARGET_VIEW_DESC* pDesc,
                                                          ID3D11RenderTargetView** ppView)
{
	HookedObj* h = dev_of(This);
	HRESULT hr = DEV_ORIG(h)->CreateRenderTargetView(This, pResource, pDesc, ppView);
	if (SUCCEEDED(hr) && !g_swap)
		try_discover_swapchain(pResource);
	return hr;
}

bool hooks_install_device(ID3D11Device* dev)
{
	HookedObj* installed = find_obj(dev);
	if (installed && installed->role == ROLE_DEVICE && *(void***)dev == installed->clone)
		return true;
	HookedObj* h = new_obj(dev, sizeof(ID3D11DeviceVtbl) / sizeof(void*), ROLE_DEVICE);
	if (!h)
		return false;
	ID3D11DeviceVtbl* v = (ID3D11DeviceVtbl*)h->clone;
	v->CreateBuffer = H_CreateBuffer;
	v->CreateTexture1D = H_CreateTexture1D;
	v->CreateTexture2D = H_CreateTexture2D;
	v->CreateTexture3D = H_CreateTexture3D;
	v->CreateVertexShader = H_CreateVertexShader;
	v->CreateHullShader = H_CreateHullShader;
	v->CreateDomainShader = H_CreateDomainShader;
	v->CreateGeometryShader = H_CreateGeometryShader;
	v->CreateGeometryShaderWithStreamOutput = H_CreateGeometryShaderWithStreamOutput;
	v->CreatePixelShader = H_CreatePixelShader;
	v->CreateComputeShader = H_CreateComputeShader;
	v->CreateInputLayout = H_CreateInputLayout;
	v->CreateRenderTargetView = H_CreateRenderTargetView;
	D3D_FEATURE_LEVEL level = ((ID3D11DeviceVtbl*)h->orig)->GetFeatureLevel(dev);
	activate(h);
	plog("hooks: device %p hooked (%zu vtable entries, feature level 0x%x)", dev, h->n, (unsigned)level);
	return true;
}

bool hooks_attach_device(ID3D11Device* dev, ID3D11DeviceContext* ctx)
{
	AcquireSRWLockExclusive(&g_target_lock);
	bool ok = hooks_install_context(ctx) && hooks_install_device(dev);
	if (ok && !g_dev) {
		// Preserve the attached status before a back buffer is seen. No capture
		// can start until hook_swapchain binds the actual rendering device.
		g_dev = find_obj(dev);
		g_ctx = find_obj(ctx);
		ID3D11Device_AddRef(dev);
		ID3D11DeviceContext_AddRef(ctx);
		g_ctx_has_ctx1 = g_ctx->context1;
		g_feature_level = ((ID3D11DeviceVtbl*)g_dev->orig)->GetFeatureLevel(dev);
	}
	ReleaseSRWLockExclusive(&g_target_lock);
	return ok;
}

// ================================================================ context
static inline HookedObj* ctx_of(void* This)
{
	if (g_ctx && g_ctx->obj == This)
		return g_ctx;
	return find_obj(This);
}
#define CTX_ORIG(h)  ((ID3D11DeviceContextVtbl*)(h)->orig)
#define CTX1_ORIG(h) ((ID3D11DeviceContext1Vtbl*)(h)->orig)

#define RECORD_DRAW(type_, args_, tg_, ibuf_, ioff_) \
	if (g_recording && h == g_ctx) { \
		CALLER(ci); \
		cap_on_draw(type_, args_, tg_, ibuf_, ioff_, ci); \
	}

static void discover_render_targets(UINT count, ID3D11RenderTargetView* const* views)
{
	if (g_swap || !views || count == D3D11_KEEP_RENDER_TARGETS_AND_DEPTH_STENCIL)
		return;
	for (UINT i = 0; i < count && i < D3D11_SIMULTANEOUS_RENDER_TARGET_COUNT && !g_swap; i++) {
		if (!views[i]) continue;
		ID3D11Resource* res = nullptr;
		ID3D11RenderTargetView_GetResource(views[i], &res);
		if (res) {
			try_discover_swapchain(res);
			ID3D11Resource_Release(res);
		}
	}
}

static void STDMETHODCALLTYPE H_Draw(ID3D11DeviceContext* This, UINT VertexCount, UINT StartVertexLocation)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW; a.vertex_count = VertexCount; a.first_vertex = StartVertexLocation;
		RECORD_DRAW(DT_DRAW, a, nullptr, nullptr, 0);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->Draw(This, VertexCount, StartVertexLocation);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawIndexed(ID3D11DeviceContext* This, UINT IndexCount, UINT StartIndexLocation,
                                            INT BaseVertexLocation)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_INDEXED; a.index_count = IndexCount; a.first_index = StartIndexLocation;
		a.first_vertex = (uint32_t)BaseVertexLocation;
		RECORD_DRAW(DT_DRAW_INDEXED, a, nullptr, nullptr, 0);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawIndexed(This, IndexCount, StartIndexLocation, BaseVertexLocation);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawInstanced(ID3D11DeviceContext* This, UINT VertexCountPerInstance, UINT InstanceCount,
                                              UINT StartVertexLocation, UINT StartInstanceLocation)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_INSTANCED; a.vertex_count = VertexCountPerInstance; a.instance_count = InstanceCount;
		a.first_vertex = StartVertexLocation; a.first_instance = StartInstanceLocation;
		RECORD_DRAW(DT_DRAW_INSTANCED, a, nullptr, nullptr, 0);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawInstanced(This, VertexCountPerInstance, InstanceCount, StartVertexLocation, StartInstanceLocation);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawIndexedInstanced(ID3D11DeviceContext* This, UINT IndexCountPerInstance,
                                                     UINT InstanceCount, UINT StartIndexLocation,
                                                     INT BaseVertexLocation, UINT StartInstanceLocation)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_INDEXED_INSTANCED; a.index_count = IndexCountPerInstance;
		a.instance_count = InstanceCount; a.first_index = StartIndexLocation;
		a.first_vertex = (uint32_t)BaseVertexLocation; a.first_instance = StartInstanceLocation;
		RECORD_DRAW(DT_DRAW_INDEXED_INSTANCED, a, nullptr, nullptr, 0);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawIndexedInstanced(This, IndexCountPerInstance, InstanceCount, StartIndexLocation,
	                                  BaseVertexLocation, StartInstanceLocation);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawAuto(ID3D11DeviceContext* This)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_AUTO;
		RECORD_DRAW(DT_DRAW_AUTO, a, nullptr, nullptr, 0);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawAuto(This);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawIndexedInstancedIndirect(ID3D11DeviceContext* This, ID3D11Buffer* pArgs, UINT Offset)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_INDEXED_INSTANCED_INDIRECT;
		RECORD_DRAW(DT_DRAW_INDEXED_INSTANCED_INDIRECT, a, nullptr, pArgs, Offset);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawIndexedInstancedIndirect(This, pArgs, Offset);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_DrawInstancedIndirect(ID3D11DeviceContext* This, ID3D11Buffer* pArgs, UINT Offset)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DRAW_INSTANCED_INDIRECT;
		RECORD_DRAW(DT_DRAW_INSTANCED_INDIRECT, a, nullptr, pArgs, Offset);
		if (g_tracking) cap_gpu_writes_om();
	}
	CTX_ORIG(h)->DrawInstancedIndirect(This, pArgs, Offset);
	if (g_rt_after && h == g_ctx) cap_after_draw();
}

static void STDMETHODCALLTYPE H_Dispatch(ID3D11DeviceContext* This, UINT X, UINT Y, UINT Z)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DISPATCH;
		uint32_t tg[3] = { X, Y, Z };
		RECORD_DRAW(DT_DISPATCH, a, tg, nullptr, 0);
		if (g_tracking) cap_gpu_writes_cs();
	}
	CTX_ORIG(h)->Dispatch(This, X, Y, Z);
}

static void STDMETHODCALLTYPE H_DispatchIndirect(ID3D11DeviceContext* This, ID3D11Buffer* pArgs, UINT Offset)
{
	HookedObj* h = ctx_of(This);
	g_hstats.draws++;
	if (h == g_ctx) g_hstats.tracked_draws++;
	if (g_swap && h == g_ctx && (g_recording || g_tracking)) {
		DrawArgs a; a.type = DT_DISPATCH_INDIRECT;
		RECORD_DRAW(DT_DISPATCH_INDIRECT, a, nullptr, pArgs, Offset);
		if (g_tracking) cap_gpu_writes_cs();
	}
	CTX_ORIG(h)->DispatchIndirect(This, pArgs, Offset);
}

// ---- writes (only looked at while a client is connected)
static HRESULT STDMETHODCALLTYPE H_Map(ID3D11DeviceContext* This, ID3D11Resource* pResource, UINT Subresource,
                                       D3D11_MAP MapType, UINT MapFlags, D3D11_MAPPED_SUBRESOURCE* pMapped)
{
	HookedObj* h = ctx_of(This);
	g_hstats.maps++;
	return CTX_ORIG(h)->Map(This, pResource, Subresource, MapType, MapFlags, pMapped);
}

static void STDMETHODCALLTYPE H_Unmap(ID3D11DeviceContext* This, ID3D11Resource* pResource, UINT Subresource)
{
	HookedObj* h = ctx_of(This);
	// The new contents become visible to the GPU here.
	if (g_swap && g_tracking && h == g_ctx)
		cap_buffer_written(pResource);
	CTX_ORIG(h)->Unmap(This, pResource, Subresource);
}

static void STDMETHODCALLTYPE H_UpdateSubresource(ID3D11DeviceContext* This, ID3D11Resource* pDst, UINT DstSub,
                                                  const D3D11_BOX* pBox, const void* pData, UINT RowPitch, UINT DepthPitch)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx) {
		if (pBox) cap_buffer_written(pDst, pBox->left, pBox->right - pBox->left);
		else cap_buffer_written(pDst);
	}
	CTX_ORIG(h)->UpdateSubresource(This, pDst, DstSub, pBox, pData, RowPitch, DepthPitch);
}

static void STDMETHODCALLTYPE H_CopyResource(ID3D11DeviceContext* This, ID3D11Resource* pDst, ID3D11Resource* pSrc)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_buffer_written(pDst);
	CTX_ORIG(h)->CopyResource(This, pDst, pSrc);
}

static void STDMETHODCALLTYPE H_CopySubresourceRegion(ID3D11DeviceContext* This, ID3D11Resource* pDst, UINT DstSub,
                                                      UINT DstX, UINT DstY, UINT DstZ, ID3D11Resource* pSrc,
                                                      UINT SrcSub, const D3D11_BOX* pSrcBox)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx) {
		// The game fills its shared mesh buffers this way: only the bytes that
		// are actually replaced lose their cached hashes.
		if (pSrcBox) cap_buffer_written(pDst, DstX, pSrcBox->right - pSrcBox->left);
		else cap_buffer_written(pDst, DstX, 0xffffffffu - DstX);
	}
	CTX_ORIG(h)->CopySubresourceRegion(This, pDst, DstSub, DstX, DstY, DstZ, pSrc, SrcSub, pSrcBox);
}

static void STDMETHODCALLTYPE H_CopyStructureCount(ID3D11DeviceContext* This, ID3D11Buffer* pDst, UINT DstOffset,
                                                   ID3D11UnorderedAccessView* pSrcView)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_buffer_written(pDst, DstOffset, 4);
	CTX_ORIG(h)->CopyStructureCount(This, pDst, DstOffset, pSrcView);
}

static void STDMETHODCALLTYPE H_UpdateSubresource1(ID3D11DeviceContext1* This, ID3D11Resource* pDst, UINT DstSub,
                                                   const D3D11_BOX* pBox, const void* pData, UINT RowPitch,
                                                   UINT DepthPitch, UINT CopyFlags)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx) {
		if (pBox) cap_buffer_written(pDst, pBox->left, pBox->right - pBox->left);
		else cap_buffer_written(pDst);
	}
	CTX1_ORIG(h)->UpdateSubresource1(This, pDst, DstSub, pBox, pData, RowPitch, DepthPitch, CopyFlags);
}

static void STDMETHODCALLTYPE H_CopySubresourceRegion1(ID3D11DeviceContext1* This, ID3D11Resource* pDst, UINT DstSub,
                                                       UINT DstX, UINT DstY, UINT DstZ, ID3D11Resource* pSrc,
                                                       UINT SrcSub, const D3D11_BOX* pSrcBox, UINT CopyFlags)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx) {
		if (pSrcBox) cap_buffer_written(pDst, DstX, pSrcBox->right - pSrcBox->left);
		else cap_buffer_written(pDst, DstX, 0xffffffffu - DstX);
	}
	CTX1_ORIG(h)->CopySubresourceRegion1(This, pDst, DstSub, DstX, DstY, DstZ, pSrc, SrcSub, pSrcBox, CopyFlags);
}

static void STDMETHODCALLTYPE H_DiscardResource(ID3D11DeviceContext1* This, ID3D11Resource* pResource)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_buffer_written(pResource);
	CTX1_ORIG(h)->DiscardResource(This, pResource);
}

// ---- GPU-side writers
static void STDMETHODCALLTYPE H_CSSetUnorderedAccessViews(ID3D11DeviceContext* This, UINT StartSlot, UINT NumUAVs,
                                                          ID3D11UnorderedAccessView* const* ppUAVs,
                                                          const UINT* pInitialCounts)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_set_cs_uavs(StartSlot, NumUAVs, ppUAVs);
	CTX_ORIG(h)->CSSetUnorderedAccessViews(This, StartSlot, NumUAVs, ppUAVs, pInitialCounts);
}

static void STDMETHODCALLTYPE H_OMSetRenderTargets(ID3D11DeviceContext* This, UINT NumViews,
                                                   ID3D11RenderTargetView* const* ppRTVs, ID3D11DepthStencilView* pDSV)
{
	HookedObj* h = ctx_of(This);
	discover_render_targets(NumViews, ppRTVs);
	if (g_swap && g_tracking && h == g_ctx)
		cap_set_om_uavs(0, 0, nullptr);          // this call unbinds all OM UAVs
	CTX_ORIG(h)->OMSetRenderTargets(This, NumViews, ppRTVs, pDSV);
}

static void STDMETHODCALLTYPE H_OMSetRenderTargetsAndUnorderedAccessViews(
	ID3D11DeviceContext* This, UINT NumRTVs, ID3D11RenderTargetView* const* ppRTVs, ID3D11DepthStencilView* pDSV,
	UINT UAVStartSlot, UINT NumUAVs, ID3D11UnorderedAccessView* const* ppUAVs, const UINT* pUAVInitialCounts)
{
	HookedObj* h = ctx_of(This);
	discover_render_targets(NumRTVs, ppRTVs);
	if (g_swap && g_tracking && h == g_ctx && NumUAVs != D3D11_KEEP_UNORDERED_ACCESS_VIEWS)
		cap_set_om_uavs(UAVStartSlot, NumUAVs, ppUAVs);
	CTX_ORIG(h)->OMSetRenderTargetsAndUnorderedAccessViews(This, NumRTVs, ppRTVs, pDSV, UAVStartSlot, NumUAVs,
	                                                       ppUAVs, pUAVInitialCounts);
}

static void STDMETHODCALLTYPE H_SOSetTargets(ID3D11DeviceContext* This, UINT NumBuffers,
                                             ID3D11Buffer* const* ppTargets, const UINT* pOffsets)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_set_so_targets(NumBuffers, ppTargets);
	CTX_ORIG(h)->SOSetTargets(This, NumBuffers, ppTargets, pOffsets);
}

static void STDMETHODCALLTYPE H_ClearState(ID3D11DeviceContext* This)
{
	HookedObj* h = ctx_of(This);
	if (g_swap && g_tracking && h == g_ctx)
		cap_state_cleared();
	CTX_ORIG(h)->ClearState(This);
}

static void STDMETHODCALLTYPE H_ExecuteCommandList(ID3D11DeviceContext* This, ID3D11CommandList* pList, BOOL Restore)
{
	HookedObj* h = ctx_of(This);
	g_hstats.exec_cmdlists++;
	// Whatever the list wrote is invisible to us: forget everything we cached.
	if (g_swap && g_tracking && h == g_ctx)
		cap_invalidate_all();
	CTX_ORIG(h)->ExecuteCommandList(This, pList, Restore);
}

static void apply_context_hooks(HookedObj* h, bool ctx1)
{
	ID3D11DeviceContextVtbl* v = (ID3D11DeviceContextVtbl*)h->clone;
	v->Draw = H_Draw;
	v->DrawIndexed = H_DrawIndexed;
	v->DrawInstanced = H_DrawInstanced;
	v->DrawIndexedInstanced = H_DrawIndexedInstanced;
	v->DrawAuto = H_DrawAuto;
	v->DrawIndexedInstancedIndirect = H_DrawIndexedInstancedIndirect;
	v->DrawInstancedIndirect = H_DrawInstancedIndirect;
	v->Dispatch = H_Dispatch;
	v->DispatchIndirect = H_DispatchIndirect;
	v->Map = H_Map;
	v->Unmap = H_Unmap;
	v->UpdateSubresource = H_UpdateSubresource;
	v->CopyResource = H_CopyResource;
	v->CopySubresourceRegion = H_CopySubresourceRegion;
	v->CopyStructureCount = H_CopyStructureCount;
	v->CSSetUnorderedAccessViews = H_CSSetUnorderedAccessViews;
	v->OMSetRenderTargets = H_OMSetRenderTargets;
	v->OMSetRenderTargetsAndUnorderedAccessViews = H_OMSetRenderTargetsAndUnorderedAccessViews;
	v->SOSetTargets = H_SOSetTargets;
	v->ClearState = H_ClearState;
	v->ExecuteCommandList = H_ExecuteCommandList;
	if (ctx1) {
		ID3D11DeviceContext1Vtbl* v1 = (ID3D11DeviceContext1Vtbl*)h->clone;
		v1->UpdateSubresource1 = H_UpdateSubresource1;
		v1->CopySubresourceRegion1 = H_CopySubresourceRegion1;
		v1->DiscardResource = H_DiscardResource;
	}
}

bool hooks_install_context(ID3D11DeviceContext* ctx)
{
	HookedObj* installed = find_obj(ctx);
	if (installed && installed->role == ROLE_CONTEXT && *(void***)ctx == installed->clone)
		return true;
	HookedObj* h = new_obj(ctx, sizeof(ID3D11DeviceContextVtbl) / sizeof(void*), ROLE_CONTEXT);
	if (!h)
		return false;

	bool ctx1 = false;
	ID3D11DeviceContext1* c1 = nullptr;
	if (SUCCEEDED(ID3D11DeviceContext_QueryInterface(ctx, IID_ID3D11DeviceContext1, (void**)&c1)) && c1) {
		ctx1 = ((void*)c1 == (void*)ctx) && h->n >= sizeof(ID3D11DeviceContext1Vtbl) / sizeof(void*);
		ID3D11DeviceContext1_Release(c1);
	}
	apply_context_hooks(h, ctx1);
	h->context1 = ctx1;
	activate(h);
	plog("hooks: immediate context %p hooked (%zu vtable entries, context1=%d)", ctx, h->n, (int)ctx1);
	return true;
}

// ================================================================ upkeep
static void reapply(HookedObj* h, const char* what)
{
	void* cur = nullptr;
	if (!h || !h->obj || !read_ptr((void* const*)h->obj, &cur))
		return;
	if (cur == (void*)h->clone)
		return;
	// The runtime installed a different table (it does so when it switches the
	// object between implementations). Follow it.
	plog("hooks: %s vtable changed %p -> %p, re-applying", what, h->orig, cur);
	HookedObj fresh = *h;
	size_t min_entries = h->role == ROLE_CONTEXT ? sizeof(ID3D11DeviceContextVtbl) / sizeof(void*)
	                   : h->role == ROLE_DEVICE ? sizeof(ID3D11DeviceVtbl) / sizeof(void*)
	                   : sizeof(IDXGISwapChainVtbl) / sizeof(void*);
	if (!clone_into(&fresh, h->obj, min_entries))
		return;
	if (h->role == ROLE_CONTEXT) {
		apply_context_hooks(&fresh, g_ctx_has_ctx1 && fresh.n >= sizeof(ID3D11DeviceContext1Vtbl) / sizeof(void*));
	} else {
		// Device and swap chain tables are not expected to change; leave them be.
		HeapFree(GetProcessHeap(), 0, fresh.clone - 1);
		return;
	}
	h->orig = fresh.orig;
	h->clone = fresh.clone;
	h->n = fresh.n;
	activate(h);
	g_hstats.revptr++;
}

void hooks_verify()
{
	reapply(g_ctx, "context");
}
