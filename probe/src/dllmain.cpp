#include "common.h"
#include "capture.h"
#include "registry.h"

BOOL WINAPI DllMain(HINSTANCE self, DWORD reason, LPVOID)
{
	if (reason == DLL_PROCESS_ATTACH) {
		DisableThreadLibraryCalls(self);
		// Stay loaded for the lifetime of the process: objects keep pointing at
		// our vtable copies.
		HMODULE pin = nullptr;
		GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
		                   (LPCWSTR)DllMain, &pin);

		log_init(self);
		wchar_t exe[MAX_PATH] = {};
		GetModuleFileNameW(nullptr, exe, MAX_PATH);
		plog("efmi probe %s loaded into %s (pid %lu)", PROBE_VERSION, narrow(exe).c_str(), GetCurrentProcessId());

		settings_load();
		reg_init();
		cap_init();
		bootstrap_start();
		ipc_start();
	}
	return TRUE;
}
