// Background texture fingerprints. Compute into eight private bytes using an unhooked
// deferred context; ExecuteCommandList(TRUE) preserves the game's complete pipeline state.
// Never flush or wait for GPU readback. No helper process or connected MCP client is needed.
#include "slot_dhash.h"
#include "slotcache.h"
#include "hooks.h"
#include "registry.h"
#include "json.h"
#include "../../shared/dhash_shader.h"
#include <d3dcompiler.h>
#include <d3d11shader.h>
#include <map>
#include <array>
#include <tuple>
#include <algorithm>

using Key = std::tuple<uint64_t, uint64_t, uint32_t, uint32_t>;
struct Input {
    ID3D11Resource* resource = nullptr;
    Key key;
    DXGI_FORMAT format = DXGI_FORMAT_UNKNOWN;
    uint32_t mip = 0, width = 0, height = 0;
    ~Input() { if (resource) ID3D11Resource_Release(resource); }
};
struct PendingHash {
    std::shared_ptr<SlotDHash> result;
    ID3D11Buffer* staging;
    uint32_t frame;
};
static std::map<Key, std::shared_ptr<SlotDHash>> hashes;
static std::map<void*, std::pair<uint64_t, uint64_t>> versions;
static std::map<uint64_t, std::array<bool, 128>> declarations;
static std::vector<PendingHash> pending;
static ID3D11Device* device = nullptr;
static ID3D11DeviceContext* deferred_context = nullptr;
static ID3D11ComputeShader* cs = nullptr;
static ID3D11SamplerState* sampler = nullptr;
static ID3D11Buffer* output = nullptr;
static ID3D11UnorderedAccessView* uav = nullptr;
static HRESULT init_error = S_OK;
static uint32_t issued_frame = 0;
static uint64_t revision = 0;
static volatile LONG64 submitted = 0, completed = 0, failed = 0, deferred_count = 0;
static volatile LONG pending_count = 0;
static pD3DCompile compile_shader = nullptr;
typedef HRESULT (WINAPI* ReflectFn)(LPCVOID, SIZE_T, REFIID, void**);
static ReflectFn reflect_shader = nullptr;

static bool compiler()
{
    static bool tried = false;
    if (!tried) {
        tried = true;
        HMODULE dll = LoadLibraryExW(L"d3dcompiler_47.dll", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (dll) {
            compile_shader = (pD3DCompile)GetProcAddress(dll, "D3DCompile");
            reflect_shader = (ReflectFn)GetProcAddress(dll, "D3DReflect");
        }
    }
    return compile_shader && reflect_shader;
}

static bool declared(uint64_t ps, unsigned slot)
{
    if (slot >= 128 || !ps) return false;
    auto found = declarations.find(ps);
    if (found == declarations.end()) {
        std::array<bool, 128> mask = {};
        std::vector<uint8_t> bytes;
        std::string type;
        ID3D11ShaderReflection* reflection = nullptr;
        if (compiler() && reg_get_bytecode(ps, &bytes, &type) &&
            SUCCEEDED(reflect_shader(bytes.data(), bytes.size(), __uuidof(ID3D11ShaderReflection), (void**)&reflection))) {
            D3D11_SHADER_DESC desc = {};
            if (SUCCEEDED(reflection->lpVtbl->GetDesc(reflection, &desc))) {
                for (UINT i = 0; i < desc.BoundResources; ++i) {
                    D3D11_SHADER_INPUT_BIND_DESC d = {};
                    if (SUCCEEDED(reflection->lpVtbl->GetResourceBindingDesc(reflection, i, &d)) &&
                        d.Type == D3D_SIT_TEXTURE && d.Dimension == D3D_SRV_DIMENSION_TEXTURE2D)
                        for (UINT j = d.BindPoint; j < 128 && j - d.BindPoint < d.BindCount; ++j) mask[j] = true;
                }
            }
            reflection->lpVtbl->Release(reflection);
        } else {
            InterlockedIncrement64(&failed); // missing bytecode/reflection: never guess a declared slot
        }
        if (declarations.size() >= 8192) declarations.clear();
        found = declarations.emplace(ps, mask).first;
    }
    return found->second[slot];
}

static bool input(ID3D11ShaderResourceView* srv, uint64_t ps, unsigned slot, Input* out)
{
    if (!slot_dhash_enabled() || !srv || !declared(ps, slot)) return false;
    D3D11_SHADER_RESOURCE_VIEW_DESC view = {};
    ID3D11ShaderResourceView_GetDesc(srv, &view);
    const UINT f = view.Format;
    // BC material textures only; no buffers, depth, render targets, arrays or cube maps.
    if (view.ViewDimension != D3D11_SRV_DIMENSION_TEXTURE2D ||
        !((f >= 71 && f <= 84 && f != 73 && f != 76 && f != 79 && f != 82) || f == 98 || f == 99)) return false;
    ID3D11ShaderResourceView_GetResource(srv, &out->resource);
    ResInfo info;
    if (!reg_get_resource(out->resource, &info) || info.origin != ORIGIN_GAME ||
        info.dim != D3D11_RESOURCE_DIMENSION_TEXTURE2D || info.desc.t2.ArraySize != 1 || info.desc.t2.SampleDesc.Count != 1)
        return false;
    const auto& d = info.desc.t2;
    UINT first = view.Texture2D.MostDetailedMip;
    UINT count = std::min(view.Texture2D.MipLevels, d.MipLevels > first ? d.MipLevels - first : 0);
    if (!count) return false;
    out->mip = first;
    for (UINT i = first; i < first + count; ++i)
        if (std::max(1u, d.Width >> i) == 256 && std::max(1u, d.Height >> i) == 256) { out->mip = i; break; }
    out->width = std::max(1u, d.Width >> out->mip);
    out->height = std::max(1u, d.Height >> out->mip);
    out->format = view.Format;
    auto& ver = versions[out->resource];
    if (ver.first != info.serial) ver = { info.serial, ++revision };
    out->key = { info.serial, ver.second, (UINT)view.Format, out->mip };
    return true;
}

uint64_t slot_dhash_signature(ID3D11ShaderResourceView* srv, uint64_t ps, unsigned slot)
{
    Input in;
    if (!input(srv, ps, slot, &in)) return 0;
    return (std::get<0>(in.key) * 0x9e3779b97f4a7c15ull) ^
           (std::get<1>(in.key) * 0xc2b2ae3d27d4eb4full) ^ ((uint64_t)in.format << 16) ^ in.mip;
}

void slot_dhash_written(ID3D11Resource* resource)
{
    // Only already-observed material resources are tracked; no registry work per write.
    auto it = versions.find(resource);
    if (it != versions.end()) it->second.second = ++revision;
}
void slot_dhash_unknown_writes()
{
    for (auto& entry : versions) entry.second.second = ++revision;
}

static void release_gpu()
{
    if (deferred_context) ID3D11DeviceContext_Release(deferred_context);
    if (cs) ID3D11ComputeShader_Release(cs);
    if (sampler) ID3D11SamplerState_Release(sampler);
    if (uav) ID3D11UnorderedAccessView_Release(uav);
    if (output) ID3D11Buffer_Release(output);
    deferred_context = nullptr; cs = nullptr; sampler = nullptr; uav = nullptr; output = nullptr;
}

#define TRY(x) do { HRESULT hr = (x); if (FAILED(hr)) return hr; } while (0)
static HRESULT init_gpu()
{
    if (cs) return S_OK;
    if (!compiler()) return E_NOINTERFACE;
    ID3DBlob* code = nullptr;
    HRESULT hr = compile_shader(shader, sizeof(shader), nullptr, nullptr, nullptr, "main", "cs_5_0",
                                D3DCOMPILE_ENABLE_STRICTNESS, 0, &code, nullptr);
    if (FAILED(hr)) return hr;
    hr = ODEV(CreateComputeShader)(rdev(), ID3D10Blob_GetBufferPointer(code), ID3D10Blob_GetBufferSize(code), nullptr, &cs);
    ID3D10Blob_Release(code);
    if (FAILED(hr)) return hr;
    TRY(ODEV(CreateDeferredContext)(rdev(), 0, &deferred_context));
    D3D11_SAMPLER_DESC sd = {};
    sd.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
    sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    sd.ComparisonFunc = D3D11_COMPARISON_ALWAYS;
    TRY(ODEV(CreateSamplerState)(rdev(), &sd, &sampler));
    D3D11_BUFFER_DESC bd = {};
    bd.ByteWidth = 8; bd.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
    bd.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; bd.StructureByteStride = 4;
    TRY(ODEV(CreateBuffer)(rdev(), &bd, nullptr, &output));
    D3D11_UNORDERED_ACCESS_VIEW_DESC ud = {};
    ud.ViewDimension = D3D11_UAV_DIMENSION_BUFFER; ud.Buffer.NumElements = 2;
    TRY(ODEV(CreateUnorderedAccessView)(rdev(), (ID3D11Resource*)output, &ud, &uav));
    return S_OK;
}

std::shared_ptr<SlotDHash> slot_dhash_request(ID3D11ShaderResourceView* srv, uint64_t ps, unsigned slot, bool* deferred)
{
    Input in;
    if (!input(srv, ps, slot, &in)) return {};
    auto found = hashes.find(in.key);
    if (found != hashes.end()) return found->second;
    if (issued_frame >= slot_dhash_max_new() || pending.size() >= 32) {
        *deferred = true;
        InterlockedIncrement64(&deferred_count);
        return {};
    }
    ++issued_frame;
    auto result = std::make_shared<SlotDHash>();
    result->mip = in.mip; result->width = in.width; result->height = in.height;
    hashes[in.key] = result;
    if (SUCCEEDED(init_error)) init_error = init_gpu();
    HRESULT hr = init_error;
    ID3D11ShaderResourceView* view = nullptr;
    ID3D11Buffer* staging = nullptr;
    ID3D11CommandList* list = nullptr;
    if (SUCCEEDED(hr)) {
        D3D11_SHADER_RESOURCE_VIEW_DESC vd = {};
        vd.Format = in.format; vd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        vd.Texture2D.MostDetailedMip = in.mip; vd.Texture2D.MipLevels = 1;
        hr = ODEV(CreateShaderResourceView)(rdev(), in.resource, &vd, &view);
    }
    if (SUCCEEDED(hr)) {
        D3D11_BUFFER_DESC bd = {};
        bd.ByteWidth = 8; bd.Usage = D3D11_USAGE_STAGING; bd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        hr = ODEV(CreateBuffer)(rdev(), &bd, nullptr, &staging);
    }
    if (SUCCEEDED(hr)) {
        ID3D11DeviceContext_CSSetShader(deferred_context, cs, nullptr, 0);
        ID3D11DeviceContext_CSSetShaderResources(deferred_context, 0, 1, &view);
        ID3D11DeviceContext_CSSetSamplers(deferred_context, 0, 1, &sampler);
        ID3D11DeviceContext_CSSetUnorderedAccessViews(deferred_context, 0, 1, &uav, nullptr);
        ID3D11DeviceContext_Dispatch(deferred_context, 1, 1, 1);
        ID3D11DeviceContext_CopyResource(deferred_context, (ID3D11Resource*)staging, (ID3D11Resource*)output);
        hr = ID3D11DeviceContext_FinishCommandList(deferred_context, FALSE, &list);
        if (SUCCEEDED(hr)) {
            OCTX(ExecuteCommandList)(rctx(), list, TRUE);
            pending.push_back({ result, staging, (uint32_t)g_frame_no });
            staging = nullptr;
            InterlockedIncrement64(&submitted);
            InterlockedExchange(&pending_count, (LONG)pending.size());
        }
    }
    if (list) ID3D11CommandList_Release(list);
    if (view) ID3D11ShaderResourceView_Release(view);
    if (staging) ID3D11Buffer_Release(staging);
    if (FAILED(hr)) {
        result->ready = true; result->error = hr;
        InterlockedIncrement64(&failed);
    }
    return result;
}

void slot_dhash_boundary()
{
    issued_frame = 0;
    for (size_t i = 0; i < pending.size();) {
        auto& p = pending[i];
        D3D11_MAPPED_SUBRESOURCE map = {};
        HRESULT hr = device != rdev() ? E_ABORT : OCTX(Map)(rctx(), (ID3D11Resource*)p.staging, 0,
                                                          D3D11_MAP_READ, D3D11_MAP_FLAG_DO_NOT_WAIT, &map);
        if (hr == DXGI_ERROR_WAS_STILL_DRAWING && (uint32_t)g_frame_no - p.frame < 120) { ++i; continue; }
        if (SUCCEEDED(hr)) {
            memcpy(&p.result->value, map.pData, 8);
            OCTX(Unmap)(rctx(), (ID3D11Resource*)p.staging, 0);
            InterlockedIncrement64(&completed);
        } else InterlockedIncrement64(&failed);
        p.result->error = hr; p.result->ready = true;
        ID3D11Buffer_Release(p.staging);
        pending.erase(pending.begin() + i);
    }
    InterlockedExchange(&pending_count, (LONG)pending.size());
    if (device != rdev()) {
        release_gpu(); hashes.clear(); versions.clear();
        device = rdev(); init_error = S_OK;
    }
    if (hashes.size() > 16384) hashes.clear();
    if (versions.size() > 16384) versions.clear();
}

void slot_dhash_status(std::string* out)
{
    JsonW w; w.begin_obj();
    w.ku("submitted", submitted); w.ku("completed", completed); w.ku("failed", failed);
    w.ku("pending", pending_count); w.ku("deferred", deferred_count);
    w.ks("source", "probe_gpu"); w.kb("xxmi_cached", false);
    w.end_obj(); *out = w.s;
}
