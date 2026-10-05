// Instantiates the interface IDs used by the probe, so that no import library
// (and therefore no static dependency on d3d11.dll / dxgi.dll) is needed.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define CINTERFACE
#define COBJMACROS
#define D3D11_NO_HELPERS
#include <windows.h>
#include <initguid.h>
#include <d3d11_4.h>
#include <dxgi1_4.h>
