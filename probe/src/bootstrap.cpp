// Getting hold of the real D3D11 device without touching XXMI or the game.
//
// XXMI poses as d3d11.dll for the game and then asks the *system* d3d11.dll for
// D3D11CreateDevice through GetProcAddress. We redirect that one export table
// entry of the system DLL (in memory only) to a pass-through function, which
// tells us which device and context were created. From then on everything is
// done by swapping vtable pointers on those objects (see hooks.cpp).
#include "common.h"
#include "hooks.h"
#include "capture.h"
#include "registry.h"
#include <psapi.h>
#include <string.h>

const char* g_attach_how = nullptr;
bool g_attach_wrapped = false;
wchar_t g_runtime_path[MAX_PATH] = {};

typedef HRESULT (WINAPI *tCreateDevice)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT, const D3D_FEATURE_LEVEL*,
                                        UINT, UINT, ID3D11Device**, D3D_FEATURE_LEVEL*, ID3D11DeviceContext**);
typedef HRESULT (WINAPI *tCreateDeviceAndSwapChain)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT,
                                                    const D3D_FEATURE_LEVEL*, UINT, UINT, const DXGI_SWAP_CHAIN_DESC*,
                                                    IDXGISwapChain**, ID3D11Device**, D3D_FEATURE_LEVEL*,
                                                    ID3D11DeviceContext**);

static tCreateDevice g_orig_create = nullptr;
static tCreateDeviceAndSwapChain g_orig_create_sc = nullptr;
static HMODULE g_patched_module = nullptr;
static SRWLOCK g_attach_lock = SRWLOCK_INIT;
static SRWLOCK g_patch_lock = SRWLOCK_INIT;

// ---------------------------------------------------------------- attach
void probe_attach(IUnknown* device, IUnknown* context, const char* how)
{
	if (!device)
		return;
	AcquireSRWLockExclusive(&g_attach_lock);
	hooks_find_xxmi();

	// XXMI's wrapper answers IUnknown with the runtime's own object, so this
	// yields the real device whether we were handed the wrapper or not.
	IUnknown* unk = nullptr;
	ID3D11Device* real = nullptr;
	if (FAILED(IUnknown_QueryInterface(device, IID_IUnknown, (void**)&unk)) || !unk) {
		plog("attach: QueryInterface(IUnknown) failed");
		ReleaseSRWLockExclusive(&g_attach_lock);
		return;
	}
	IUnknown_QueryInterface(unk, IID_ID3D11Device, (void**)&real);
	IUnknown_Release(unk);
	if (!real) {
		plog("attach: object is not a D3D11 device");
		ReleaseSRWLockExclusive(&g_attach_lock);
		return;
	}

	bool wrapped = ((void*)real != (void*)device);
	ModuleRange vm;
	if (module_from_address(*(void**)real, &vm)) {
		wcscpy_s(g_runtime_path, vm.path);
		if (!is_system_module_path(vm.path))
			plog("attach: WARNING device vtable lives in %s, not in the system runtime", narrow(vm.path).c_str());
	}

	if (g_dev && g_dev->obj == (void*)real) {
		plog("attach: device %p already hooked (%s)", real, how);
	} else {
		if (g_dev)
			plog("attach: additional device %p; capture target remains paired with its swap chain", real);
		ID3D11DeviceContext* rc = nullptr;
		((ID3D11DeviceVtbl*)real->lpVtbl)->GetImmediateContext(real, &rc);
		plog("attach: via %s, given device %p (context %p), real device %p, real context %p, wrapped=%d",
		     how, device, context, real, rc, (int)wrapped);
		if (rc) {
			if (hooks_attach_device(real, rc)) {
				g_attach_how = how;
				g_attach_wrapped = wrapped;
			}
			ID3D11DeviceContext_Release(rc);
		}
	}
	ID3D11Device_Release(real);
	ReleaseSRWLockExclusive(&g_attach_lock);
}

static HRESULT WINAPI Hook_CreateDevice(IDXGIAdapter* pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software,
                                        UINT Flags, const D3D_FEATURE_LEVEL* pLevels, UINT nLevels, UINT SDKVersion,
                                        ID3D11Device** ppDevice, D3D_FEATURE_LEVEL* pLevel,
                                        ID3D11DeviceContext** ppContext)
{
	HRESULT hr = g_orig_create(pAdapter, DriverType, Software, Flags, pLevels, nLevels, SDKVersion,
	                           ppDevice, pLevel, ppContext);
	if (SUCCEEDED(hr) && ppDevice && *ppDevice)
		probe_attach((IUnknown*)*ppDevice, ppContext ? (IUnknown*)*ppContext : nullptr, "D3D11CreateDevice");
	return hr;
}

static HRESULT WINAPI Hook_CreateDeviceAndSwapChain(IDXGIAdapter* pAdapter, D3D_DRIVER_TYPE DriverType, HMODULE Software,
                                                    UINT Flags, const D3D_FEATURE_LEVEL* pLevels, UINT nLevels,
                                                    UINT SDKVersion, const DXGI_SWAP_CHAIN_DESC* pSwapDesc,
                                                    IDXGISwapChain** ppSwapChain, ID3D11Device** ppDevice,
                                                    D3D_FEATURE_LEVEL* pLevel, ID3D11DeviceContext** ppContext)
{
	HRESULT hr = g_orig_create_sc(pAdapter, DriverType, Software, Flags, pLevels, nLevels, SDKVersion, pSwapDesc,
	                              ppSwapChain, ppDevice, pLevel, ppContext);
	if (SUCCEEDED(hr) && ppDevice && *ppDevice)
		probe_attach((IUnknown*)*ppDevice, ppContext ? (IUnknown*)*ppContext : nullptr, "D3D11CreateDeviceAndSwapChain");
	return hr;
}

// ---------------------------------------------------------------- export redirection
// Export table entries are 32-bit offsets from the module base, so the jump
// stub has to live within 4 GB above it.
static void* alloc_near(HMODULE mod)
{
	const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)mod;
	const IMAGE_NT_HEADERS* nt = (const IMAGE_NT_HEADERS*)((const BYTE*)mod + dos->e_lfanew);
	uintptr_t base = (uintptr_t)mod;
	uintptr_t addr = (base + nt->OptionalHeader.SizeOfImage + 0xFFFF) & ~(uintptr_t)0xFFFF;
	uintptr_t limit = base + 0x7FF00000ull;
	while (addr < limit) {
		MEMORY_BASIC_INFORMATION mbi;
		if (!VirtualQuery((void*)addr, &mbi, sizeof(mbi)))
			break;
		uintptr_t region = (uintptr_t)mbi.BaseAddress;
		if (mbi.State == MEM_FREE) {
			uintptr_t a = (region + 0xFFFF) & ~(uintptr_t)0xFFFF;
			if (a + 0x1000 <= region + mbi.RegionSize && a < limit) {
				void* p = VirtualAlloc((void*)a, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
				if (p)
					return p;
			}
		}
		addr = region + mbi.RegionSize;
	}
	return nullptr;
}

static BYTE* write_stub(BYTE* at, void* target)
{
	// mov rax, imm64 ; jmp rax
	at[0] = 0x48; at[1] = 0xB8;
	memcpy(at + 2, &target, 8);
	at[10] = 0xFF; at[11] = 0xE0;
	return at + 16;
}

static bool redirect_export(HMODULE mod, const char* name, void* stub, void** original)
{
	BYTE* base = (BYTE*)mod;
	const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)mod;
	const IMAGE_NT_HEADERS* nt = (const IMAGE_NT_HEADERS*)(base + dos->e_lfanew);
	const IMAGE_DATA_DIRECTORY& dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];
	if (!dir.VirtualAddress)
		return false;
	const IMAGE_EXPORT_DIRECTORY* exp = (const IMAGE_EXPORT_DIRECTORY*)(base + dir.VirtualAddress);
	const DWORD* names = (const DWORD*)(base + exp->AddressOfNames);
	const WORD* ordinals = (const WORD*)(base + exp->AddressOfNameOrdinals);
	DWORD* funcs = (DWORD*)(base + exp->AddressOfFunctions);

	for (DWORD i = 0; i < exp->NumberOfNames; i++) {
		if (strcmp((const char*)(base + names[i]), name))
			continue;
		DWORD* slot = &funcs[ordinals[i]];
		if (*slot >= dir.VirtualAddress && *slot < dir.VirtualAddress + dir.Size)
			return false;                              // forwarder, not code
		*original = base + *slot;
		DWORD old = 0;
		if (!VirtualProtect(slot, sizeof(DWORD), PAGE_READWRITE, &old))
			return false;
		*slot = (DWORD)((BYTE*)stub - base);
		VirtualProtect(slot, sizeof(DWORD), old, &old);
		return true;
	}
	return false;
}

static void patch_runtime(HMODULE mod, const wchar_t* path)
{
	AcquireSRWLockExclusive(&g_patch_lock);
	if (g_patched_module == mod) {
		ReleaseSRWLockExclusive(&g_patch_lock);
		return;
	}
	BYTE* page = (BYTE*)alloc_near(mod);
	if (!page) {
		plog("bootstrap: no free memory near %s", narrow(path).c_str());
		ReleaseSRWLockExclusive(&g_patch_lock);
		return;
	}
	BYTE* stub_cd = page;
	BYTE* stub_sc = write_stub(stub_cd, (void*)Hook_CreateDevice);
	write_stub(stub_sc, (void*)Hook_CreateDeviceAndSwapChain);
	DWORD old = 0;
	VirtualProtect(page, 0x1000, PAGE_EXECUTE_READ, &old);
	FlushInstructionCache(GetCurrentProcess(), page, 0x1000);

	void* o1 = nullptr;
	void* o2 = nullptr;
	bool a = redirect_export(mod, "D3D11CreateDevice", stub_cd, &o1);
	if (a) g_orig_create = (tCreateDevice)o1;
	bool b = redirect_export(mod, "D3D11CreateDeviceAndSwapChain", stub_sc, &o2);
	if (b) g_orig_create_sc = (tCreateDeviceAndSwapChain)o2;
	g_patched_module = mod;
	plog("bootstrap: %s at %p, export redirection D3D11CreateDevice=%d D3D11CreateDeviceAndSwapChain=%d",
	     narrow(path).c_str(), mod, (int)a, (int)b);
	ReleaseSRWLockExclusive(&g_patch_lock);
}

static bool is_runtime(const wchar_t* path)
{
	const wchar_t* base = wcsrchr(path, L'\\');
	base = base ? base + 1 : path;
	return !_wcsicmp(base, L"d3d11.dll") && is_system_module_path(path);
}

// ---------------------------------------------------------------- loader notification
struct USTR { USHORT Length; USHORT MaximumLength; PWSTR Buffer; };
struct LDR_NOTIFY_DATA { ULONG Flags; const USTR* FullDllName; const USTR* BaseDllName; PVOID DllBase; ULONG SizeOfImage; };
typedef VOID (CALLBACK *LDR_NOTIFY_FN)(ULONG reason, const LDR_NOTIFY_DATA* data, PVOID context);
typedef LONG (NTAPI *tLdrRegisterDllNotification)(ULONG flags, LDR_NOTIFY_FN fn, PVOID context, PVOID* cookie);

static VOID CALLBACK on_dll_event(ULONG reason, const LDR_NOTIFY_DATA* data, PVOID)
{
	if (reason != 1 || !data || !data->FullDllName || !data->FullDllName->Buffer)   // 1 = loaded
		return;
	wchar_t path[MAX_PATH];
	size_t n = data->FullDllName->Length / sizeof(wchar_t);
	if (n >= MAX_PATH)
		return;
	memcpy(path, data->FullDllName->Buffer, n * sizeof(wchar_t));
	path[n] = 0;
	if (is_runtime(path))
		patch_runtime((HMODULE)data->DllBase, path);
}

void bootstrap_start()
{
	// Be told when the system d3d11.dll gets mapped: the redirection has to be
	// in place before anybody looks the export up.
	HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
	tLdrRegisterDllNotification reg =
		ntdll ? (tLdrRegisterDllNotification)GetProcAddress(ntdll, "LdrRegisterDllNotification") : nullptr;
	PVOID cookie = nullptr;
	LONG st = reg ? reg(0, on_dll_event, nullptr, &cookie) : -1;
	plog("bootstrap: loader notification %s (0x%lx)", st == 0 ? "registered" : "FAILED", st);

	// It may already be there.
	HMODULE mods[1024];
	DWORD needed = 0;
	bool found = false;
	if (K32EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &needed)) {
		DWORD count = needed / sizeof(HMODULE);
		if (count > _countof(mods)) count = _countof(mods);
		for (DWORD i = 0; i < count; i++) {
			wchar_t path[MAX_PATH];
			if (GetModuleFileNameW(mods[i], path, MAX_PATH) && is_runtime(path)) {
				plog("bootstrap: system d3d11.dll was already loaded when the probe started");
				patch_runtime(mods[i], path);
				found = true;
			}
		}
	}
	if (!found)
		plog("bootstrap: system d3d11.dll not loaded yet, waiting for it");
}

// ---------------------------------------------------------------- exports
extern "C" __declspec(dllexport) int EfmiProbeAttach(IUnknown* device, IUnknown* context)
{
	probe_attach(device, context, "EfmiProbeAttach");
	return (g_dev && g_ctx) ? 1 : 0;
}

extern "C" __declspec(dllexport) const char* EfmiProbeVersion()
{
	return PROBE_VERSION;
}
