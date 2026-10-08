// EFMI probe sandbox.
//
// A small D3D11 program that behaves like the game where it matters to XXMI and
// to the probe, so both can be exercised together without launching the game:
//   * d3d11.dll is resolved at run time with LoadLibrary/GetProcAddress, after
//     the "injected" DLLs are in place (XXMI first, then the probe);
//   * meshes live in one big shared buffer and are drawn through bind offsets,
//     StartIndexLocation and BaseVertexLocation;
//   * constants come from a 64 KB buffer bound in 16-constant windows;
//   * textures are created with initial data in several formats.
//
// It never touches the real EFMI folder: it is pointed at a private copy.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11_1.h>
#include <dxgi1_2.h>
#include <d3dcompiler.h>
#include <stdio.h>
#include <stdarg.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <algorithm>
#include <string>
#include <vector>

typedef HRESULT (WINAPI *tD3D11CreateDevice)(IDXGIAdapter*, D3D_DRIVER_TYPE, HMODULE, UINT, const D3D_FEATURE_LEVEL*,
                                             UINT, UINT, ID3D11Device**, D3D_FEATURE_LEVEL*, ID3D11DeviceContext**);

static FILE* g_log = nullptr;
static void say(const char* fmt, ...)
{
	char buf[1024];
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	printf("%s\n", buf);
	fflush(stdout);
	if (g_log) { fprintf(g_log, "%s\n", buf); fflush(g_log); }
}

static std::string narrow(const wchar_t* w)
{
	char b[1024];
	WideCharToMultiByte(CP_UTF8, 0, w, -1, b, sizeof(b), nullptr, nullptr);
	return b;
}

static std::string module_of(const void* addr)
{
	HMODULE h = nullptr;
	wchar_t path[MAX_PATH] = L"?";
	if (GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
	                       (LPCWSTR)addr, &h) && h)
		GetModuleFileNameW(h, path, MAX_PATH);
	return narrow(path);
}

#define CHECK(hr, what) do { if (FAILED(hr)) { say("FAILED %s: 0x%08x", what, (unsigned)(hr)); return false; } } while (0)

// ------------------------------------------------------------------ shaders
static const char* k_hlsl = R"(
cbuffer PerObject : register(b0)
{
	float4x4 mvp;
	float4   tint;
	float4   misc;
	float4   pad[10];
};
Texture2D    t_albedo : register(t0);
Texture2D    t_detail : register(t1);
Texture2D    t_mask   : register(t2);
Texture1D    t_ramp   : register(t3);
Texture3D    t_volume : register(t4);
Texture2D    t_odd    : register(t5);
TextureCube  t_cube   : register(t6);
SamplerState s_linear : register(s0);

struct VSIn  { float3 pos : POSITION; float3 nrm : NORMAL; float2 uv : TEXCOORD0; float4 col : COLOR0; uint iid : SV_InstanceID; };
struct VSOut { float4 pos : SV_Position; float3 nrm : NORMAL; float2 uv : TEXCOORD0; float4 col : COLOR0; };

VSOut vs_main(VSIn i)
{
	VSOut o;
	float3 p = i.pos + float3(0, 0.18 * i.iid, 0);
	o.pos = mul(float4(p, 1), mvp);
	o.nrm = i.nrm;
	o.uv = i.uv;
	o.col = i.col * tint;
	return o;
}

float4 ps_main(VSOut i) : SV_Target
{
	float4 a = t_albedo.Sample(s_linear, i.uv);
	float4 d = t_detail.Sample(s_linear, i.uv * 4);
	float4 m = t_mask.Sample(s_linear, i.uv);
	float4 r = t_ramp.Sample(s_linear, i.uv.x);
	float  v = t_volume.Sample(s_linear, float3(i.uv, 0.5)).r;
	float  o = t_odd.Sample(s_linear, i.uv).r;
	float4 c = t_cube.Sample(s_linear, normalize(i.nrm + float3(0.01, 0.02, 1)));
	float3 rgb = a.rgb * i.col.rgb * (0.6 + 0.4 * d.rgb) + 0.05 * m.rgb + 0.05 * r.rgb + 0.03 * v + 0.03 * o + 0.03 * c.rgb;
	return float4(rgb, 1);
}

float4 vs_blit(uint id : SV_VertexID, out float2 uv : TEXCOORD0) : SV_Position
{
	uv = float2((id << 1) & 2, id & 2);
	return float4(uv * float2(2, -2) + float2(-1, 1), 0, 1);
}

float4 ps_blit(float4 pos : SV_Position, float2 uv : TEXCOORD0) : SV_Target
{
	return t_albedo.Sample(s_linear, uv);
}

cbuffer Pose : register(b0)
{
	float4 pose;   // x = time, y = vertex count, z = grid
};
RWByteAddressBuffer u_out : register(u0);

[numthreads(64, 1, 1)]
void cs_pose(uint3 id : SV_DispatchThreadID)
{
	uint i = id.x;
	if (i >= (uint)pose.y) return;
	uint n = (uint)pose.z;
	// This scene is a byte-for-byte hash fixture. Without precise, positions
	// were observed to change by 1 ULP between startup and later frames with
	// identical constants (also without XXMI and with the old probe). Preserve
	// the arithmetic instead of hiding the real byte changes with a warmup.
	precise float fx = (float)(i % (n + 1)) / n;
	precise float fy = (float)(i / (n + 1)) / n;
	precise float3 p = float3(fx * 0.5 + 0.35, fy * 0.5 - 0.85, 0.5 + 0.03 * sin(pose.x + fx * 6.28));
	uint o = i * 32;
	u_out.Store3(o, asuint(p));
	u_out.Store3(o + 12, asuint(float3(0, 0, 1)));
	u_out.Store2(o + 24, asuint(float2(fx, fy)));
}
)";

static bool compile(const char* entry, const char* target, ID3DBlob** out, const char* source = k_hlsl)
{
	ID3DBlob* err = nullptr;
	HRESULT hr = D3DCompile(source, strlen(source), "sandbox.hlsl", nullptr, nullptr, entry, target,
	                        D3DCOMPILE_OPTIMIZATION_LEVEL1, 0, out, &err);
	if (FAILED(hr)) {
		say("shader %s failed: %s", entry, err ? (const char*)err->GetBufferPointer() : "?");
		if (err) err->Release();
		return false;
	}
	if (err) err->Release();
	return true;
}

// ------------------------------------------------------------------ scene data
struct V0 { float pos[3]; float nrm[3]; float uv[2]; };
struct V1 { float col[4]; };

struct Mesh {
	std::vector<V0> v0;
	std::vector<V1> v1;
	std::vector<uint16_t> idx;
	UINT off_v0 = 0, off_v1 = 0, off_i = 0;
};

static void make_grid(Mesh* m, int n, float ox, float oy, float size, float r, float g, float b)
{
	for (int y = 0; y <= n; y++) {
		for (int x = 0; x <= n; x++) {
			V0 v;
			float fx = (float)x / n, fy = (float)y / n;
			v.pos[0] = ox + fx * size;
			v.pos[1] = oy + fy * size;
			v.pos[2] = 0.5f + 0.04f * sinf(fx * 9.0f) * cosf(fy * 7.0f);
			v.nrm[0] = 0; v.nrm[1] = 0; v.nrm[2] = 1;
			v.uv[0] = fx; v.uv[1] = fy;
			m->v0.push_back(v);
			V1 c = { { r * (0.5f + 0.5f * fx), g * (0.5f + 0.5f * fy), b, 1.0f } };
			m->v1.push_back(c);
		}
	}
	for (int y = 0; y < n; y++) {
		for (int x = 0; x < n; x++) {
			uint16_t a = (uint16_t)(y * (n + 1) + x);
			uint16_t bq = a + 1, c = (uint16_t)(a + n + 1), d = c + 1;
			m->idx.push_back(a); m->idx.push_back(c); m->idx.push_back(bq);
			m->idx.push_back(bq); m->idx.push_back(c); m->idx.push_back(d);
		}
	}
}

static bool g_plain_draw = false;           // --plain-draw: use Draw() instead of DrawInstanced()
static bool g_stream = false;               // --stream: keep adding unrelated data to the shared buffer
static bool g_rewrite_mesh1 = false;        // --rewrite-mesh1: re-upload mesh 1's indices every frame
static int g_recreate_mesh3 = 0;            // --recreate-mesh3 N: every N frames destroy mesh 3's buffers and
                                            // create new ones of the same sizes holding other bytes
static int g_repeat = 1;                    // --repeat: issue the mesh draws this many times per frame
static bool g_state_pass = false;           // --state-pass: one more draw, indirect, with all fixed-function
                                            // state set and views that show a part of their resource
static int g_vsync = 1;                     // --novsync sets this to 0
static unsigned g_skip = 0;                 // --skip bit mask, for bisecting a scene
static uint32_t g_lcg = 0x12345678;
static uint8_t rnd8() { g_lcg = g_lcg * 1664525u + 1013904223u; return (uint8_t)(g_lcg >> 24); }

struct Scene {
	ID3D11Device* dev = nullptr;
	ID3D11DeviceContext* ctx = nullptr;
	ID3D11DeviceContext1* ctx1 = nullptr;
	IDXGISwapChain1* swap = nullptr;
	UINT width = 0, height = 0;

	ID3D11Buffer* heap = nullptr;        // shared VB+IB storage
	ID3D11Buffer* upload = nullptr;      // dynamic staging used to fill the heap
	ID3D11Buffer* cb_heap = nullptr;     // 64 KB constant buffer
	ID3D11Buffer* cb_pose = nullptr;
	ID3D11Buffer* vb3a = nullptr, *vb3b = nullptr, *ib3 = nullptr;
	ID3D11Buffer* posed = nullptr;
	ID3D11UnorderedAccessView* posed_uav = nullptr;

	Mesh m1, m2, m3;

	ID3D11VertexShader* vs = nullptr, *vs_blit = nullptr;
	ID3D11PixelShader* ps = nullptr, *ps_blit = nullptr;
	ID3D11ComputeShader* cs = nullptr;
	ID3D11InputLayout* layout = nullptr;
	ID3D11SamplerState* sampler = nullptr;

	ID3D11ShaderResourceView* srv[7] = {};
	ID3D11Texture2D* scene_tex = nullptr;
	ID3D11RenderTargetView* scene_rtv = nullptr;
	ID3D11ShaderResourceView* scene_srv = nullptr;
	ID3D11Texture2D* depth_tex = nullptr;
	ID3D11DepthStencilView* dsv = nullptr;
	ID3D11RenderTargetView* back_rtv = nullptr;
	ID3D11DepthStencilState* dss = nullptr;
	ID3D11RasterizerState* rs = nullptr;

	// --state-pass
	ID3D11BlendState* x_blend = nullptr;
	ID3D11DepthStencilState* x_dss = nullptr;
	ID3D11RasterizerState* x_rs = nullptr;
	ID3D11SamplerState* x_cmp = nullptr, *x_aniso = nullptr;
	ID3D11ShaderResourceView* x_tex_part = nullptr, *x_buf_part = nullptr;
	ID3D11RenderTargetView* x_rtv_mip1 = nullptr;
	ID3D11Buffer* x_args = nullptr;
};
static bool build_state_pass(Scene& s);

// Mesh 3 in its own immutable buffers (hash includes the data), 32-bit indices.
// A variant other than 0 keeps every size and changes the bytes: the vertices
// move a little, the colours fade and each triangle starts at another corner.
static bool create_mesh3(Scene& s, int variant)
{
	std::vector<V0> v0 = s.m3.v0;
	std::vector<V1> v1 = s.m3.v1;
	std::vector<uint32_t> idx32(s.m3.idx.begin(), s.m3.idx.end());
	for (V0& v : v0) v.pos[2] += 0.0005f * (float)variant;
	for (V1& c : v1) c.col[3] -= 0.001f * (float)variant;
	for (size_t i = 0; variant % 3 && i + 2 < idx32.size(); i += 3)
		std::rotate(idx32.begin() + i, idx32.begin() + i + variant % 3, idx32.begin() + i + 3);

	D3D11_BUFFER_DESC d = {};
	D3D11_SUBRESOURCE_DATA init = {};
	HRESULT hr;
	d.Usage = D3D11_USAGE_IMMUTABLE;
	d.BindFlags = D3D11_BIND_VERTEX_BUFFER;
	d.ByteWidth = (UINT)(v0.size() * sizeof(V0)); init.pSysMem = v0.data();
	hr = s.dev->CreateBuffer(&d, &init, &s.vb3a); CHECK(hr, "vb3a");
	d.ByteWidth = (UINT)(v1.size() * sizeof(V1)); init.pSysMem = v1.data();
	hr = s.dev->CreateBuffer(&d, &init, &s.vb3b); CHECK(hr, "vb3b");
	d.BindFlags = D3D11_BIND_INDEX_BUFFER;
	d.ByteWidth = (UINT)(idx32.size() * 4); init.pSysMem = idx32.data();
	hr = s.dev->CreateBuffer(&d, &init, &s.ib3); CHECK(hr, "ib3");
	return true;
}

// Destroys mesh 3's buffers before creating the next variant, so that the new
// objects can be given the addresses of the old ones.
static bool recreate_mesh3(Scene& s, int variant, int frame)
{
	ID3D11Buffer* none[2] = { nullptr, nullptr };
	UINT zero[2] = { 0, 0 };
	s.ctx->IASetVertexBuffers(0, 2, none, zero, zero);
	s.ctx->IASetIndexBuffer(nullptr, DXGI_FORMAT_UNKNOWN, 0);
	void* old_vb = s.vb3a;
	void* old_ib = s.ib3;
	s.vb3a->Release(); s.vb3b->Release(); s.ib3->Release();
	s.vb3a = s.vb3b = s.ib3 = nullptr;
	s.ctx->Flush();
	if (!create_mesh3(s, variant))
		return false;
	say("mesh3 recreated frame=%d variant=%d vb %p -> %p ib %p -> %p", frame, variant, old_vb, (void*)s.vb3a, old_ib, (void*)s.ib3);
	return true;
}

static bool heap_write(Scene& s, UINT dst_off, const void* data, UINT size)
{
	D3D11_MAPPED_SUBRESOURCE m;
	HRESULT hr = s.ctx->Map(s.upload, 0, D3D11_MAP_WRITE_DISCARD, 0, &m);
	CHECK(hr, "Map upload");
	memcpy(m.pData, data, size);
	s.ctx->Unmap(s.upload, 0);
	D3D11_BOX box = { 0, 0, 0, size, 1, 1 };
	s.ctx->CopySubresourceRegion(s.heap, 0, dst_off, 0, 0, s.upload, 0, &box);
	return true;
}

static bool make_tex2d(Scene& s, UINT w, UINT h, UINT mips, UINT array, DXGI_FORMAT fmt, UINT misc,
                       const std::vector<D3D11_SUBRESOURCE_DATA>& init, ID3D11ShaderResourceView** srv)
{
	D3D11_TEXTURE2D_DESC d = {};
	d.Width = w; d.Height = h; d.MipLevels = mips; d.ArraySize = array; d.Format = fmt;
	d.SampleDesc.Count = 1; d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE; d.MiscFlags = misc;
	ID3D11Texture2D* t = nullptr;
	HRESULT hr = s.dev->CreateTexture2D(&d, init.data(), &t);
	CHECK(hr, "CreateTexture2D");
	hr = s.dev->CreateShaderResourceView(t, nullptr, srv);
	t->Release();
	CHECK(hr, "CreateShaderResourceView");
	return true;
}

static bool build_scene(Scene& s)
{
	HRESULT hr;

	// ---- shaders
	ID3DBlob* b_vs = nullptr, *b_ps = nullptr, *b_vsb = nullptr, *b_psb = nullptr, *b_cs = nullptr;
	if (!compile("vs_main", "vs_5_0", &b_vs) || !compile("ps_main", "ps_5_0", &b_ps) ||
	    !compile("vs_blit", "vs_5_0", &b_vsb) || !compile("ps_blit", "ps_5_0", &b_psb) ||
	    !compile("cs_pose", "cs_5_0", &b_cs))
		return false;
	hr = s.dev->CreateVertexShader(b_vs->GetBufferPointer(), b_vs->GetBufferSize(), nullptr, &s.vs); CHECK(hr, "vs");
	hr = s.dev->CreatePixelShader(b_ps->GetBufferPointer(), b_ps->GetBufferSize(), nullptr, &s.ps); CHECK(hr, "ps");
	hr = s.dev->CreateVertexShader(b_vsb->GetBufferPointer(), b_vsb->GetBufferSize(), nullptr, &s.vs_blit); CHECK(hr, "vs_blit");
	hr = s.dev->CreatePixelShader(b_psb->GetBufferPointer(), b_psb->GetBufferSize(), nullptr, &s.ps_blit); CHECK(hr, "ps_blit");
	hr = s.dev->CreateComputeShader(b_cs->GetBufferPointer(), b_cs->GetBufferSize(), nullptr, &s.cs); CHECK(hr, "cs");

	const D3D11_INPUT_ELEMENT_DESC il[] = {
		{ "POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT,    0, 0,  D3D11_INPUT_PER_VERTEX_DATA, 0 },
		{ "NORMAL",   0, DXGI_FORMAT_R32G32B32_FLOAT,    0, 12, D3D11_INPUT_PER_VERTEX_DATA, 0 },
		{ "TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT,       0, 24, D3D11_INPUT_PER_VERTEX_DATA, 0 },
		{ "COLOR",    0, DXGI_FORMAT_R32G32B32A32_FLOAT, 1, 0,  D3D11_INPUT_PER_VERTEX_DATA, 0 },
	};
	hr = s.dev->CreateInputLayout(il, 4, b_vs->GetBufferPointer(), b_vs->GetBufferSize(), &s.layout); CHECK(hr, "layout");
	b_vs->Release(); b_ps->Release(); b_vsb->Release(); b_psb->Release(); b_cs->Release();

	// ---- meshes in the shared heap
	make_grid(&s.m1, 12, -0.95f, -0.05f, 0.55f, 1.0f, 0.4f, 0.3f);
	make_grid(&s.m2, 20, -0.30f, -0.05f, 0.55f, 0.3f, 1.0f, 0.4f);
	make_grid(&s.m3, 7,  -0.95f, -0.85f, 0.55f, 0.4f, 0.5f, 1.0f);

	D3D11_BUFFER_DESC bd = {};
	bd.ByteWidth = 4 * 1024 * 1024;
	bd.Usage = D3D11_USAGE_DEFAULT;
	bd.BindFlags = D3D11_BIND_VERTEX_BUFFER | D3D11_BIND_INDEX_BUFFER;
	hr = s.dev->CreateBuffer(&bd, nullptr, &s.heap); CHECK(hr, "heap");

	bd.ByteWidth = 256 * 1024;
	bd.Usage = D3D11_USAGE_DYNAMIC;
	bd.BindFlags = D3D11_BIND_VERTEX_BUFFER;
	bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
	hr = s.dev->CreateBuffer(&bd, nullptr, &s.upload); CHECK(hr, "upload");

	UINT cursor = 13664;                       // deliberately not zero, like the game
	Mesh* heap_meshes[2] = { &s.m1, &s.m2 };
	for (Mesh* m : heap_meshes) {
		m->off_v0 = cursor; cursor += (UINT)(m->v0.size() * sizeof(V0)); cursor = (cursor + 31) & ~31u;
		m->off_v1 = cursor; cursor += (UINT)(m->v1.size() * sizeof(V1)); cursor = (cursor + 31) & ~31u;
		m->off_i = cursor;  cursor += (UINT)(m->idx.size() * sizeof(uint16_t)); cursor = (cursor + 63) & ~63u;
		if (!heap_write(s, m->off_v0, m->v0.data(), (UINT)(m->v0.size() * sizeof(V0)))) return false;
		if (!heap_write(s, m->off_v1, m->v1.data(), (UINT)(m->v1.size() * sizeof(V1)))) return false;
		if (!heap_write(s, m->off_i, m->idx.data(), (UINT)(m->idx.size() * sizeof(uint16_t)))) return false;
	}

	// ---- a mesh in its own immutable buffers
	if (!create_mesh3(s, 0))
		return false;

	// ---- a vertex buffer written by a compute shader every frame
	{
		D3D11_BUFFER_DESC d = {};
		d.ByteWidth = (UINT)(s.m2.v0.size() * sizeof(V0));
		d.Usage = D3D11_USAGE_DEFAULT;
		d.BindFlags = D3D11_BIND_VERTEX_BUFFER | D3D11_BIND_UNORDERED_ACCESS;
		d.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_ALLOW_RAW_VIEWS;
		hr = s.dev->CreateBuffer(&d, nullptr, &s.posed); CHECK(hr, "posed");
		D3D11_UNORDERED_ACCESS_VIEW_DESC u = {};
		u.Format = DXGI_FORMAT_R32_TYPELESS;
		u.ViewDimension = D3D11_UAV_DIMENSION_BUFFER;
		u.Buffer.NumElements = d.ByteWidth / 4;
		u.Buffer.Flags = D3D11_BUFFER_UAV_FLAG_RAW;
		hr = s.dev->CreateUnorderedAccessView(s.posed, &u, &s.posed_uav); CHECK(hr, "posed uav");
	}

	// ---- constants
	bd = {};
	bd.ByteWidth = 65536;
	bd.Usage = D3D11_USAGE_DYNAMIC;
	bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
	bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
	hr = s.dev->CreateBuffer(&bd, nullptr, &s.cb_heap); CHECK(hr, "cb heap");
	bd.ByteWidth = 16;
	hr = s.dev->CreateBuffer(&bd, nullptr, &s.cb_pose); CHECK(hr, "cb pose");

	// ---- textures with initial data
	{   // t0: RGBA8 sRGB with a full mip chain
		std::vector<std::vector<uint8_t>> mips;
		std::vector<D3D11_SUBRESOURCE_DATA> init;
		for (UINT w = 256; w >= 1; w /= 2) {
			std::vector<uint8_t> px(w * w * 4);
			for (UINT y = 0; y < w; y++) for (UINT x = 0; x < w; x++) {
				bool chk = ((x * 8 / w) + (y * 8 / w)) & 1;
				uint8_t* p = &px[(y * w + x) * 4];
				p[0] = chk ? 230 : 60; p[1] = (uint8_t)(x * 255 / w); p[2] = (uint8_t)(y * 255 / w); p[3] = 255;
			}
			mips.push_back(std::move(px));
		}
		UINT w = 256;
		for (auto& m : mips) { init.push_back({ m.data(), w * 4, 0 }); w = w > 1 ? w / 2 : 1; }
		if (!make_tex2d(s, 256, 256, (UINT)mips.size(), 1, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, 0, init, &s.srv[0])) return false;
	}
	{   // t1: BC1 (half a byte per texel)
		std::vector<uint8_t> blocks(128 / 4 * 128 / 4 * 8);
		for (auto& b : blocks) b = rnd8();
		std::vector<D3D11_SUBRESOURCE_DATA> init = { { blocks.data(), 128 / 4 * 8, 0 } };
		if (!make_tex2d(s, 128, 128, 1, 1, DXGI_FORMAT_BC1_UNORM, 0, init, &s.srv[1])) return false;
	}
	{   // t2: BC7 (one byte per texel)
		std::vector<uint8_t> blocks(64 / 4 * 64 / 4 * 16);
		for (auto& b : blocks) b = rnd8();
		std::vector<D3D11_SUBRESOURCE_DATA> init = { { blocks.data(), 64 / 4 * 16, 0 } };
		if (!make_tex2d(s, 64, 64, 1, 1, DXGI_FORMAT_BC7_UNORM, 0, init, &s.srv[2])) return false;
	}
	{   // t3: 1D ramp
		std::vector<uint8_t> px(256 * 4);
		for (int i = 0; i < 256; i++) { px[i * 4] = (uint8_t)i; px[i * 4 + 1] = (uint8_t)(255 - i); px[i * 4 + 2] = 128; px[i * 4 + 3] = 255; }
		D3D11_TEXTURE1D_DESC d = {};
		d.Width = 256; d.MipLevels = 1; d.ArraySize = 1; d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
		d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		D3D11_SUBRESOURCE_DATA init = { px.data(), 0, 0 };
		ID3D11Texture1D* t = nullptr;
		hr = s.dev->CreateTexture1D(&d, &init, &t); CHECK(hr, "tex1d");
		hr = s.dev->CreateShaderResourceView(t, nullptr, &s.srv[3]); t->Release(); CHECK(hr, "tex1d srv");
	}
	{   // t4: 3D volume
		std::vector<uint8_t> px(16 * 16 * 16);
		for (auto& b : px) b = rnd8();
		D3D11_TEXTURE3D_DESC d = {};
		d.Width = d.Height = d.Depth = 16; d.MipLevels = 1; d.Format = DXGI_FORMAT_R8_UNORM;
		d.Usage = D3D11_USAGE_IMMUTABLE; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		D3D11_SUBRESOURCE_DATA init = { px.data(), 16, 256 };
		ID3D11Texture3D* t = nullptr;
		hr = s.dev->CreateTexture3D(&d, &init, &t); CHECK(hr, "tex3d");
		hr = s.dev->CreateShaderResourceView(t, nullptr, &s.srv[4]); t->Release(); CHECK(hr, "tex3d srv");
	}
	{   // t5: R8 with a row pitch larger than the row
		std::vector<uint8_t> px(128 * 60, 0);
		for (int y = 0; y < 60; y++) for (int x = 0; x < 100; x++) px[y * 128 + x] = (uint8_t)(x * 2 + y);
		std::vector<D3D11_SUBRESOURCE_DATA> init = { { px.data(), 128, 0 } };
		if (!make_tex2d(s, 100, 60, 1, 1, DXGI_FORMAT_R8_UNORM, 0, init, &s.srv[5])) return false;
	}
	{   // t6: cube map (array of six)
		std::vector<std::vector<uint8_t>> faces(6, std::vector<uint8_t>(32 * 32 * 4));
		std::vector<D3D11_SUBRESOURCE_DATA> init;
		for (int f = 0; f < 6; f++) {
			for (size_t i = 0; i < faces[f].size(); i++) faces[f][i] = (uint8_t)(40 * f + (i & 63));
			init.push_back({ faces[f].data(), 32 * 4, 0 });
		}
		if (!make_tex2d(s, 32, 32, 1, 6, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_RESOURCE_MISC_TEXTURECUBE, init, &s.srv[6])) return false;
	}

	// ---- render targets (same size as the swap chain: resolution-relative hash)
	{
		D3D11_TEXTURE2D_DESC d = {};
		d.Width = s.width; d.Height = s.height; d.MipLevels = 1; d.ArraySize = 1;
		d.Format = DXGI_FORMAT_R8G8B8A8_UNORM; d.SampleDesc.Count = 1;
		d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
		hr = s.dev->CreateTexture2D(&d, nullptr, &s.scene_tex); CHECK(hr, "scene tex");
		hr = s.dev->CreateRenderTargetView(s.scene_tex, nullptr, &s.scene_rtv); CHECK(hr, "scene rtv");
		hr = s.dev->CreateShaderResourceView(s.scene_tex, nullptr, &s.scene_srv); CHECK(hr, "scene srv");
		d.Format = DXGI_FORMAT_D32_FLOAT;
		d.BindFlags = D3D11_BIND_DEPTH_STENCIL;
		hr = s.dev->CreateTexture2D(&d, nullptr, &s.depth_tex); CHECK(hr, "depth tex");
		hr = s.dev->CreateDepthStencilView(s.depth_tex, nullptr, &s.dsv); CHECK(hr, "dsv");
	}
	{
		ID3D11Texture2D* bb = nullptr;
		hr = s.swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)&bb); CHECK(hr, "GetBuffer");
		hr = s.dev->CreateRenderTargetView(bb, nullptr, &s.back_rtv);
		bb->Release();
		CHECK(hr, "back rtv");
	}

	D3D11_SAMPLER_DESC sd = {};
	sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
	sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
	sd.MaxLOD = D3D11_FLOAT32_MAX;
	hr = s.dev->CreateSamplerState(&sd, &s.sampler); CHECK(hr, "sampler");

	D3D11_DEPTH_STENCIL_DESC dd = {};
	dd.DepthEnable = FALSE;
	dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
	dd.DepthFunc = D3D11_COMPARISON_ALWAYS;
	hr = s.dev->CreateDepthStencilState(&dd, &s.dss); CHECK(hr, "dss");
	D3D11_RASTERIZER_DESC rd = {};
	rd.FillMode = D3D11_FILL_SOLID;
	rd.CullMode = D3D11_CULL_NONE;
	rd.DepthClipEnable = FALSE;
	hr = s.dev->CreateRasterizerState(&rd, &s.rs); CHECK(hr, "rs");
	if (g_state_pass && !build_state_pass(s))
		return false;
	return true;
}

struct ObjConsts { float mvp[16]; float tint[4]; float misc[4]; float pad[40]; };   // 256 bytes = 16 constants

static void bind_object(Scene& s, UINT object)
{
	if (s.ctx1) {
		UINT first = 16 * (4 + object * 3);        // windows are not contiguous, like the game's
		UINT num = 16;
		s.ctx1->VSSetConstantBuffers1(0, 1, &s.cb_heap, &first, &num);
		s.ctx1->PSSetConstantBuffers1(0, 1, &s.cb_heap, nullptr, nullptr);
	} else {
		s.ctx->VSSetConstantBuffers(0, 1, &s.cb_heap);
		s.ctx->PSSetConstantBuffers(0, 1, &s.cb_heap);
	}
}

// ------------------------------------------------------------------ --state-pass
// Values that differ from every default and from each other, so that a field
// read from the wrong place shows up in the self-test.
static bool build_state_pass(Scene& s)
{
	HRESULT hr;
	D3D11_BLEND_DESC bd = {};
	bd.RenderTarget[0].BlendEnable = TRUE;
	bd.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;
	bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
	bd.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
	bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
	bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_BLEND_FACTOR;
	bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_MAX;
	bd.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_RED | D3D11_COLOR_WRITE_ENABLE_GREEN |
	                                           D3D11_COLOR_WRITE_ENABLE_BLUE;
	hr = s.dev->CreateBlendState(&bd, &s.x_blend); CHECK(hr, "x blend");

	D3D11_DEPTH_STENCIL_DESC dd = {};
	dd.DepthEnable = TRUE;
	dd.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
	dd.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
	dd.StencilEnable = TRUE;
	dd.StencilReadMask = 0x3f;
	dd.StencilWriteMask = 0x0f;
	dd.FrontFace = { D3D11_STENCIL_OP_KEEP, D3D11_STENCIL_OP_INCR_SAT, D3D11_STENCIL_OP_REPLACE, D3D11_COMPARISON_NOT_EQUAL };
	dd.BackFace = { D3D11_STENCIL_OP_ZERO, D3D11_STENCIL_OP_DECR, D3D11_STENCIL_OP_INVERT, D3D11_COMPARISON_GREATER };
	hr = s.dev->CreateDepthStencilState(&dd, &s.x_dss); CHECK(hr, "x dss");

	D3D11_RASTERIZER_DESC rd = {};
	rd.FillMode = D3D11_FILL_SOLID;
	rd.CullMode = D3D11_CULL_FRONT;
	rd.FrontCounterClockwise = TRUE;
	rd.DepthBias = 3;
	rd.DepthBiasClamp = 0.5f;
	rd.SlopeScaledDepthBias = 1.5f;
	rd.DepthClipEnable = TRUE;
	rd.ScissorEnable = TRUE;
	hr = s.dev->CreateRasterizerState(&rd, &s.x_rs); CHECK(hr, "x rs");

	D3D11_SAMPLER_DESC sd = {};
	sd.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT;
	sd.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
	sd.AddressV = D3D11_TEXTURE_ADDRESS_BORDER;
	sd.AddressW = D3D11_TEXTURE_ADDRESS_MIRROR;
	sd.MipLODBias = 0.5f;
	sd.MaxAnisotropy = 4;
	sd.ComparisonFunc = D3D11_COMPARISON_LESS_EQUAL;
	sd.BorderColor[0] = 1.0f; sd.BorderColor[1] = 0.5f; sd.BorderColor[2] = 0.25f; sd.BorderColor[3] = 0.0f;
	sd.MinLOD = 1.0f;
	sd.MaxLOD = 7.0f;
	hr = s.dev->CreateSamplerState(&sd, &s.x_cmp); CHECK(hr, "x cmp sampler");
	sd = {};
	sd.Filter = D3D11_FILTER_ANISOTROPIC;
	sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
	sd.MaxAnisotropy = 4;
	sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
	sd.MaxLOD = 3.0f;
	hr = s.dev->CreateSamplerState(&sd, &s.x_aniso); CHECK(hr, "x anisotropic sampler");

	{   // a view of mips 1-2 and slices 1-2 of a texture array with four mips and three slices
		D3D11_TEXTURE2D_DESC d = {};
		d.Width = d.Height = 32; d.MipLevels = 4; d.ArraySize = 3;
		d.Format = DXGI_FORMAT_R8G8B8A8_UNORM; d.SampleDesc.Count = 1;
		d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		ID3D11Texture2D* t = nullptr;
		hr = s.dev->CreateTexture2D(&d, nullptr, &t); CHECK(hr, "x tex");
		D3D11_SHADER_RESOURCE_VIEW_DESC vd = {};
		vd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
		vd.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2DARRAY;
		vd.Texture2DArray.MostDetailedMip = 1; vd.Texture2DArray.MipLevels = 2;
		vd.Texture2DArray.FirstArraySlice = 1; vd.Texture2DArray.ArraySize = 2;
		hr = s.dev->CreateShaderResourceView(t, &vd, &s.x_tex_part); t->Release(); CHECK(hr, "x tex srv");
	}
	{   // elements 8..31 of a structured buffer of 64
		D3D11_BUFFER_DESC d = {};
		d.ByteWidth = 64 * 16; d.Usage = D3D11_USAGE_DEFAULT; d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
		d.MiscFlags = D3D11_RESOURCE_MISC_BUFFER_STRUCTURED; d.StructureByteStride = 16;
		ID3D11Buffer* b = nullptr;
		hr = s.dev->CreateBuffer(&d, nullptr, &b); CHECK(hr, "x buf");
		D3D11_SHADER_RESOURCE_VIEW_DESC vd = {};
		vd.Format = DXGI_FORMAT_UNKNOWN;
		vd.ViewDimension = D3D11_SRV_DIMENSION_BUFFER;
		vd.Buffer.FirstElement = 8; vd.Buffer.NumElements = 24;
		hr = s.dev->CreateShaderResourceView(b, &vd, &s.x_buf_part); b->Release(); CHECK(hr, "x buf srv");
	}
	{   // a render target that is the second mip of its texture
		D3D11_TEXTURE2D_DESC d = {};
		d.Width = 256; d.Height = 128; d.MipLevels = 2; d.ArraySize = 1;
		d.Format = DXGI_FORMAT_R8G8B8A8_UNORM; d.SampleDesc.Count = 1;
		d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
		ID3D11Texture2D* t = nullptr;
		hr = s.dev->CreateTexture2D(&d, nullptr, &t); CHECK(hr, "x rt");
		D3D11_RENDER_TARGET_VIEW_DESC vd = {};
		vd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
		vd.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
		vd.Texture2D.MipSlice = 1;
		hr = s.dev->CreateRenderTargetView(t, &vd, &s.x_rtv_mip1); t->Release(); CHECK(hr, "x rtv");
	}
	{   // indirect arguments at an offset: mesh 3, two instances
		const UINT args[10] = { 0xdead, 0xbeef, 7, 7, 7, (UINT)s.m3.idx.size(), 2, 0, 0, 0 };
		D3D11_BUFFER_DESC d = {};
		d.ByteWidth = sizeof(args); d.Usage = D3D11_USAGE_DEFAULT; d.BindFlags = D3D11_BIND_UNORDERED_ACCESS;
		d.MiscFlags = D3D11_RESOURCE_MISC_DRAWINDIRECT_ARGS;
		D3D11_SUBRESOURCE_DATA init = { args, 0, 0 };
		hr = s.dev->CreateBuffer(&d, &init, &s.x_args); CHECK(hr, "x args");
	}
	return true;
}

static void bind_object(Scene& s, UINT object);

static void render_state_pass(Scene& s)
{
	const float factor[4] = { 0.1f, 0.2f, 0.3f, 0.4f };
	s.ctx->OMSetBlendState(s.x_blend, factor, 0x0000ffff);
	s.ctx->OMSetDepthStencilState(s.x_dss, 5);
	s.ctx->RSSetState(s.x_rs);
	const D3D11_RECT rects[2] = { { 10, 20, 120, 60 }, { 5, 6, 7, 8 } };
	s.ctx->RSSetScissorRects(2, rects);
	const D3D11_VIEWPORT vps[2] = { { 0, 0, 128, 64, 0.25f, 0.75f }, { 8, 16, 64, 32, 0, 1 } };
	s.ctx->RSSetViewports(2, vps);
	s.ctx->OMSetRenderTargets(1, &s.x_rtv_mip1, nullptr);
	const float x_clear[4] = { 0.2f, 0.1f, 0.0f, 1.0f };
	s.ctx->ClearRenderTargetView(s.x_rtv_mip1, x_clear);
	s.ctx->PSSetSamplers(3, 1, &s.x_cmp);
	s.ctx->VSSetSamplers(1, 1, &s.x_aniso);
	s.ctx->PSSetShaderResources(8, 1, &s.x_tex_part);
	s.ctx->VSSetShaderResources(9, 1, &s.x_buf_part);

	ID3D11Buffer* vbs[2] = { s.vb3a, s.vb3b };
	UINT strides[2] = { sizeof(V0), sizeof(V1) };
	UINT offs[2] = { 0, 0 };
	s.ctx->IASetVertexBuffers(0, 2, vbs, strides, offs);
	s.ctx->IASetIndexBuffer(s.ib3, DXGI_FORMAT_R32_UINT, 0);
	bind_object(s, 2);
	s.ctx->DrawIndexedInstancedIndirect(s.x_args, 20);

	// the same mesh into the depth target alone, as a shadow or depth pass draws it
	s.ctx->OMSetDepthStencilState(nullptr, 0);
	s.ctx->RSSetState(s.rs);
	s.ctx->OMSetRenderTargets(0, nullptr, s.dsv);
	s.ctx->DrawIndexed((UINT)s.m3.idx.size(), 0, 0);

	// back to what the rest of the frame expects
	ID3D11ShaderResourceView* no_srv = nullptr;
	ID3D11SamplerState* no_sampler = nullptr;
	s.ctx->PSSetShaderResources(8, 1, &no_srv);
	s.ctx->VSSetShaderResources(9, 1, &no_srv);
	s.ctx->PSSetSamplers(3, 1, &no_sampler);
	s.ctx->VSSetSamplers(1, 1, &no_sampler);
	s.ctx->OMSetBlendState(nullptr, nullptr, 0xffffffff);
	s.ctx->OMSetDepthStencilState(s.dss, 0);
	s.ctx->RSSetState(s.rs);
	s.ctx->RSSetScissorRects(0, nullptr);
	D3D11_VIEWPORT vp = { 0, 0, (float)s.width, (float)s.height, 0, 1 };
	s.ctx->RSSetViewports(1, &vp);
}

static void render(Scene& s, float time)
{
	// constants for every object, written in one go
	D3D11_MAPPED_SUBRESOURCE m;
	if (SUCCEEDED(s.ctx->Map(s.cb_heap, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
		memset(m.pData, 0, 65536);
		for (UINT obj = 0; obj < 5; obj++) {
			ObjConsts c = {};
			c.mvp[0] = c.mvp[5] = c.mvp[10] = c.mvp[15] = 1.0f;
			c.tint[0] = 1.0f; c.tint[1] = 1.0f - 0.1f * obj; c.tint[2] = 0.8f + 0.04f * obj; c.tint[3] = 1.0f;
			c.misc[0] = time; c.misc[1] = (float)obj;
			UINT first = s.ctx1 ? 16 * (4 + obj * 3) : 0;
			memcpy((uint8_t*)m.pData + first * 16, &c, sizeof(c));
		}
		s.ctx->Unmap(s.cb_heap, 0);
	}

	// What the engine does while the player moves around: more mesh data keeps
	// arriving in the shared buffer, next to meshes that are already being drawn.
	if (g_stream) {
		static uint8_t junk[4096];
		static unsigned n = 0;
		for (size_t i = 0; i < sizeof(junk); i++) junk[i] = (uint8_t)(i * 7 + n);
		heap_write(s, 2 * 1024 * 1024 + (n % 64) * 8192, junk, sizeof(junk));
		n++;
	}
	if (g_rewrite_mesh1)
		heap_write(s, s.m1.off_i, s.m1.idx.data(), (UINT)(s.m1.idx.size() * sizeof(uint16_t)));

	// GPU-posed vertices
	if (SUCCEEDED(s.ctx->Map(s.cb_pose, 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
		float pose[4] = { time, (float)s.m2.v0.size(), 20.0f, 0 };
		memcpy(m.pData, pose, sizeof(pose));
		s.ctx->Unmap(s.cb_pose, 0);
	}
	s.ctx->CSSetShader(s.cs, nullptr, 0);
	s.ctx->CSSetConstantBuffers(0, 1, &s.cb_pose);
	s.ctx->CSSetUnorderedAccessViews(0, 1, &s.posed_uav, nullptr);
	if (!(g_skip & 1))
		s.ctx->Dispatch((UINT)(s.m2.v0.size() + 63) / 64, 1, 1);
	ID3D11UnorderedAccessView* no_uav = nullptr;
	s.ctx->CSSetUnorderedAccessViews(0, 1, &no_uav, nullptr);

	// scene pass
	const float clear[4] = { 0.05f, 0.06f, 0.09f, 1.0f };
	D3D11_VIEWPORT vp = { 0, 0, (float)s.width, (float)s.height, 0, 1 };
	s.ctx->RSSetViewports(1, &vp);
	s.ctx->RSSetState(s.rs);
	s.ctx->OMSetDepthStencilState(s.dss, 0);
	s.ctx->OMSetRenderTargets(1, &s.scene_rtv, s.dsv);
	s.ctx->ClearRenderTargetView(s.scene_rtv, clear);
	s.ctx->ClearDepthStencilView(s.dsv, D3D11_CLEAR_DEPTH, 1.0f, 0);

	s.ctx->IASetInputLayout(s.layout);
	s.ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
	s.ctx->VSSetShader(s.vs, nullptr, 0);
	s.ctx->PSSetShader(s.ps, nullptr, 0);
	s.ctx->PSSetShaderResources(0, 7, s.srv);
	s.ctx->PSSetSamplers(0, 1, &s.sampler);

	for (int rpt = 0; rpt < g_repeat; rpt++) {
	// 1) index buffer bound at an offset, instanced
	if (!(g_skip & 2)) {
		ID3D11Buffer* vbs[2] = { s.heap, s.heap };
		UINT strides[2] = { sizeof(V0), sizeof(V1) };
		UINT offs[2] = { s.m1.off_v0, s.m1.off_v1 };
		s.ctx->IASetVertexBuffers(0, 2, vbs, strides, offs);
		s.ctx->IASetIndexBuffer(s.heap, DXGI_FORMAT_R16_UINT, s.m1.off_i);
		bind_object(s, 0);
		s.ctx->DrawIndexedInstanced((UINT)s.m1.idx.size(), 4, 0, 0, 0);
	}
	// 2) index buffer bound at zero, selected with StartIndexLocation / BaseVertexLocation
	if (!(g_skip & 4)) {
		const INT base_vertex = 100;
		ID3D11Buffer* vbs[2] = { s.heap, s.heap };
		UINT strides[2] = { sizeof(V0), sizeof(V1) };
		UINT offs[2] = { s.m2.off_v0 - base_vertex * (UINT)sizeof(V0), s.m2.off_v1 - base_vertex * (UINT)sizeof(V1) };
		s.ctx->IASetVertexBuffers(0, 2, vbs, strides, offs);
		s.ctx->IASetIndexBuffer(s.heap, DXGI_FORMAT_R16_UINT, 0);
		bind_object(s, 1);
		s.ctx->DrawIndexedInstanced((UINT)s.m2.idx.size(), 2, s.m2.off_i / 2, base_vertex, 0);
	}
	// 3) dedicated immutable buffers, 32-bit indices
	{
		ID3D11Buffer* vbs[2] = { s.vb3a, s.vb3b };
		UINT strides[2] = { sizeof(V0), sizeof(V1) };
		UINT offs[2] = { 0, 0 };
		s.ctx->IASetVertexBuffers(0, 2, vbs, strides, offs);
		s.ctx->IASetIndexBuffer(s.ib3, DXGI_FORMAT_R32_UINT, 0);
		bind_object(s, 2);
		if (!(g_skip & 8))
			s.ctx->DrawIndexed((UINT)s.m3.idx.size(), 0, 0);
		// 4) the same vertices without an index buffer. Like the game's engine
		// this goes through DrawInstanced: XXMI 1.2.0's frame analysis never
		// returns from a plain Draw() in this program (verified with XXMI
		// alone, no probe), which would stall the comparison run.
		bind_object(s, 3);
		if (!(g_skip & 16)) {
			if (g_plain_draw) s.ctx->Draw((UINT)s.m3.v0.size() - 4, 4);
			else s.ctx->DrawInstanced((UINT)s.m3.v0.size() - 4, 1, 4, 0);
		}
	}
	}   // repeat
	// 5) compute-written positions with the shared index data
	if (!(g_skip & 32)) {
		ID3D11Buffer* vbs[2] = { s.posed, s.heap };
		UINT strides[2] = { sizeof(V0), sizeof(V1) };
		UINT offs[2] = { 0, s.m2.off_v1 };
		s.ctx->IASetVertexBuffers(0, 2, vbs, strides, offs);
		s.ctx->IASetIndexBuffer(s.heap, DXGI_FORMAT_R16_UINT, s.m2.off_i);
		bind_object(s, 4);
		s.ctx->DrawIndexed((UINT)s.m2.idx.size(), 0, 0);
	}

	if (g_state_pass)
		render_state_pass(s);

	// present pass
	ID3D11ShaderResourceView* none[7] = {};
	s.ctx->PSSetShaderResources(0, 7, none);
	s.ctx->OMSetRenderTargets(1, &s.back_rtv, nullptr);
	s.ctx->IASetInputLayout(nullptr);
	s.ctx->VSSetShader(s.vs_blit, nullptr, 0);
	s.ctx->PSSetShader(s.ps_blit, nullptr, 0);
	s.ctx->PSSetShaderResources(0, 1, &s.scene_srv);
	if (!(g_skip & 64)) {
		if (g_plain_draw) s.ctx->Draw(3, 0);
		else s.ctx->DrawInstanced(3, 1, 0, 0);
	}
	ID3D11ShaderResourceView* null_srv = nullptr;
	s.ctx->PSSetShaderResources(0, 1, &null_srv);
}

// A device with no swap chain models a helper/overlay that is created after the
// game has started. It also does real work between the main draw and Present:
// accepting either its draws or writes into the main frame is a regression.
struct AuxiliaryDevice {
	ID3D11Device* dev = nullptr;
	ID3D11DeviceContext* ctx = nullptr;
	ID3D11Buffer* vb = nullptr, *ib = nullptr, *cb = nullptr;
	ID3D11VertexShader* vs = nullptr;
	ID3D11PixelShader* ps = nullptr;
	ID3D11InputLayout* layout = nullptr;
	ID3D11RenderTargetView* rtv = nullptr;

	~AuxiliaryDevice()
	{
		if (ctx) ctx->ClearState();
		if (rtv) rtv->Release();
		if (layout) layout->Release();
		if (ps) ps->Release();
		if (vs) vs->Release();
		if (cb) cb->Release();
		if (ib) ib->Release();
		if (vb) vb->Release();
		if (ctx) ctx->Release();
		if (dev) dev->Release();
	}

	bool create(tD3D11CreateDevice create_device, IDXGIAdapter* adapter)
	{
		const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
		HRESULT hr = create_device(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, levels, 2,
		                          D3D11_SDK_VERSION, &dev, nullptr, &ctx);
		CHECK(hr, "auxiliary device");
		say("auxiliary device %p context %p (no swap chain)", dev, ctx);

		static const char* source = R"(
cbuffer AuxiliaryConstants : register(b0) { float4 marker; };
float4 aux_vs(float3 pos : POSITION) : SV_Position { return float4(pos, 1); }
float4 aux_ps(float4 pos : SV_Position) : SV_Target { return marker; }
)";
		ID3DBlob* bvs = nullptr, *bps = nullptr;
		if (!compile("aux_vs", "vs_5_0", &bvs, source)) return false;
		if (!compile("aux_ps", "ps_5_0", &bps, source)) { bvs->Release(); return false; }
		hr = dev->CreateVertexShader(bvs->GetBufferPointer(), bvs->GetBufferSize(), nullptr, &vs);
		if (SUCCEEDED(hr)) hr = dev->CreatePixelShader(bps->GetBufferPointer(), bps->GetBufferSize(), nullptr, &ps);
		const D3D11_INPUT_ELEMENT_DESC il = { "POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0,
		                                      D3D11_INPUT_PER_VERTEX_DATA, 0 };
		if (SUCCEEDED(hr)) hr = dev->CreateInputLayout(&il, 1, bvs->GetBufferPointer(), bvs->GetBufferSize(), &layout);
		bvs->Release(); bps->Release();
		CHECK(hr, "auxiliary shaders/layout");

		D3D11_BUFFER_DESC bd = {};
		bd.ByteWidth = 9 * sizeof(float); bd.Usage = D3D11_USAGE_DYNAMIC;
		bd.BindFlags = D3D11_BIND_VERTEX_BUFFER; bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
		hr = dev->CreateBuffer(&bd, nullptr, &vb); CHECK(hr, "auxiliary vb");
		const uint16_t indices[] = { 0, 2, 1 };
		D3D11_SUBRESOURCE_DATA init = { indices, 0, 0 };
		bd = {}; bd.ByteWidth = sizeof(indices); bd.Usage = D3D11_USAGE_IMMUTABLE;
		bd.BindFlags = D3D11_BIND_INDEX_BUFFER;
		hr = dev->CreateBuffer(&bd, &init, &ib); CHECK(hr, "auxiliary ib");
		bd = {}; bd.ByteWidth = 16; bd.Usage = D3D11_USAGE_DEFAULT; bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
		hr = dev->CreateBuffer(&bd, nullptr, &cb); CHECK(hr, "auxiliary cb");
		D3D11_TEXTURE2D_DESC td = {};
		td.Width = td.Height = 16; td.MipLevels = td.ArraySize = 1;
		td.Format = DXGI_FORMAT_R8G8B8A8_UNORM; td.SampleDesc.Count = 1;
		td.Usage = D3D11_USAGE_DEFAULT; td.BindFlags = D3D11_BIND_RENDER_TARGET;
		ID3D11Texture2D* tex = nullptr;
		hr = dev->CreateTexture2D(&td, nullptr, &tex); CHECK(hr, "auxiliary target");
		hr = dev->CreateRenderTargetView(tex, nullptr, &rtv); tex->Release(); CHECK(hr, "auxiliary rtv");
		return true;
	}

	bool render(int frame)
	{
		float vertices[9] = { -0.5f, -0.5f, 0.5f, 0.0f, 0.5f, 0.5f, 0.5f, -0.5f, 0.5f };
		vertices[0] += (frame % 8) * 0.001f;
		D3D11_MAPPED_SUBRESOURCE mapped = {};
		HRESULT hr = ctx->Map(vb, 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped); CHECK(hr, "auxiliary Map");
		memcpy(mapped.pData, vertices, sizeof(vertices));
		ctx->Unmap(vb, 0);
		const float marker[4] = { 0.75f, (frame % 16) / 16.0f, 0.25f, 1.0f };
		ctx->UpdateSubresource(cb, 0, nullptr, marker, 0, 0);
		UINT stride = 3 * sizeof(float), offset = 0;
		ctx->IASetVertexBuffers(0, 1, &vb, &stride, &offset);
		ctx->IASetIndexBuffer(ib, DXGI_FORMAT_R16_UINT, 0);
		ctx->IASetInputLayout(layout);
		ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
		ctx->VSSetShader(vs, nullptr, 0);
		ctx->PSSetShader(ps, nullptr, 0);
		ctx->PSSetConstantBuffers(0, 1, &cb);
		ctx->OMSetRenderTargets(1, &rtv, nullptr);
		D3D11_VIEWPORT vp = { 0, 0, 16, 16, 0, 1 };
		ctx->RSSetViewports(1, &vp);
		ctx->ClearRenderTargetView(rtv, marker);
		ctx->DrawIndexed(3, 0, 0);
		ctx->Flush();
		return true;
	}
};

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
	if (m == WM_DESTROY) { PostQuitMessage(0); return 0; }
	// DefWindowProc turns F10 key-up into SC_KEYMENU. This menu-less test
	// window must keep presenting while the real XXMI reload key is tested,
	// rather than entering the default system-menu modal loop.
	if (m == WM_SYSCOMMAND && (w & 0xfff0) == SC_KEYMENU) return 0;
	return DefWindowProcW(h, m, w, l);
}

static void press(WORD vk, bool down)
{
	INPUT in = {};
	in.type = INPUT_KEYBOARD;
	in.ki.wVk = vk;
	in.ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
	SendInput(1, &in, sizeof(in));
}

int wmain(int argc, wchar_t** argv)
{
	std::wstring xxmi, probe;
	bool probe_first = false, preload_runtime = false, animate = false, manual_attach = false;
	bool aux_device_first = false;
	int aux_device_after = -1;
	int max_frames = 0, max_seconds = 30;
	WORD dump_vk = 0x87;                              // F24
	UINT width = 1280, height = 720;

	for (int i = 1; i < argc; i++) {
		std::wstring a = argv[i];
		auto next = [&]() -> std::wstring { return i + 1 < argc ? argv[++i] : L""; };
		if (a == L"--xxmi") xxmi = next();
		else if (a == L"--probe") probe = next();
		else if (a == L"--probe-first") probe_first = true;
		else if (a == L"--preload-runtime") preload_runtime = true;
		else if (a == L"--animate") animate = true;
		else if (a == L"--manual-attach") manual_attach = true;
		else if (a == L"--aux-device-first") aux_device_first = true;
		else if (a == L"--aux-device-after") aux_device_after = _wtoi(next().c_str());
		else if (a == L"--frames") max_frames = _wtoi(next().c_str());
		else if (a == L"--plain-draw") g_plain_draw = true;
		else if (a == L"--state-pass") g_state_pass = true;
		else if (a == L"--repeat") g_repeat = _wtoi(next().c_str());
		else if (a == L"--novsync") g_vsync = 0;
		else if (a == L"--stream") g_stream = true;
		else if (a == L"--rewrite-mesh1") g_rewrite_mesh1 = true;
		else if (a == L"--recreate-mesh3") g_recreate_mesh3 = _wtoi(next().c_str());
		else if (a == L"--skip") g_skip = (unsigned)wcstoul(next().c_str(), nullptr, 0);
		else if (a == L"--seconds") max_seconds = _wtoi(next().c_str());
		else if (a == L"--dump-vk") dump_vk = (WORD)wcstoul(next().c_str(), nullptr, 0);
		else if (a == L"--size") { width = _wtoi(next().c_str()); height = _wtoi(next().c_str()); }
	}

	wchar_t exe[MAX_PATH];
	GetModuleFileNameW(nullptr, exe, MAX_PATH);
	std::wstring dir = exe;
	dir.resize(dir.find_last_of(L'\\'));
	_wfopen_s(&g_log, (dir + L"\\sandbox.log").c_str(), L"w");
	say("sandbox pid %lu", GetCurrentProcessId());

	if (preload_runtime) {
		wchar_t sys[MAX_PATH];
		GetSystemDirectoryW(sys, MAX_PATH);
		std::wstring p = std::wstring(sys) + L"\\d3d11.dll";
		say("preloading system runtime: %p", LoadLibraryW(p.c_str()));
	}
	auto load = [&](const std::wstring& path, const char* what) {
		if (path.empty()) return (HMODULE)nullptr;
		HMODULE h = LoadLibraryW(path.c_str());
		say("load %s %s -> %p (error %lu)", what, narrow(path.c_str()).c_str(), h, h ? 0 : GetLastError());
		return h;
	};
	HMODULE h_probe = nullptr;
	if (probe_first) { h_probe = load(probe, "probe"); load(xxmi, "xxmi"); }
	else             { load(xxmi, "xxmi"); h_probe = load(probe, "probe"); }

	WNDCLASSW wc = {};
	wc.lpfnWndProc = wndproc;
	wc.hInstance = GetModuleHandleW(nullptr);
	wc.lpszClassName = L"EfmiSandbox";
	wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
	RegisterClassW(&wc);
	RECT rc = { 0, 0, (LONG)width, (LONG)height };
	AdjustWindowRect(&rc, WS_OVERLAPPEDWINDOW, FALSE);
	HWND hwnd = CreateWindowW(L"EfmiSandbox", L"EFMI probe sandbox", WS_OVERLAPPEDWINDOW, 60, 60,
	                          rc.right - rc.left, rc.bottom - rc.top, nullptr, nullptr, wc.hInstance, nullptr);
	ShowWindow(hwnd, SW_SHOWNOACTIVATE);

	// Resolve the entry point the way the engine does: by name, at run time.
	HMODULE d3d = LoadLibraryW(L"d3d11.dll");
	wchar_t d3d_path[MAX_PATH] = L"?";
	if (d3d) GetModuleFileNameW(d3d, d3d_path, MAX_PATH);
	say("d3d11.dll resolved to %s", narrow(d3d_path).c_str());
	tD3D11CreateDevice create = d3d ? (tD3D11CreateDevice)GetProcAddress(d3d, "D3D11CreateDevice") : nullptr;
	if (!create) { say("no D3D11CreateDevice"); return 2; }
	say("D3D11CreateDevice at %p in %s", create, module_of(create).c_str());

	Scene s;
	AuxiliaryDevice aux;
	s.width = width; s.height = height;
	IDXGIFactory1* factory = nullptr;
	HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&factory);
	if (FAILED(hr)) { say("CreateDXGIFactory1 failed 0x%08x", (unsigned)hr); return 2; }
	IDXGIAdapter* adapter = nullptr;
	factory->EnumAdapters(0, &adapter);
	if (aux_device_first) {
		if (!aux.create(create, adapter) || !aux.render(0)) return 3;
		say("AUXILIARY READY frame=0 order=first");
	}
	const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0 };
	D3D_FEATURE_LEVEL got = (D3D_FEATURE_LEVEL)0;
	hr = create(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, levels, 2, D3D11_SDK_VERSION, &s.dev, &got, &s.ctx);
	if (FAILED(hr)) { say("D3D11CreateDevice failed 0x%08x", (unsigned)hr); return 2; }
	say("device %p (vtable in %s)", s.dev, module_of(*(void**)s.dev).c_str());
	say("context %p (vtable in %s), feature level 0x%x", s.ctx, module_of(*(void**)s.ctx).c_str(), (unsigned)got);
	s.ctx->QueryInterface(__uuidof(ID3D11DeviceContext1), (void**)&s.ctx1);
	say("ID3D11DeviceContext1: %p", s.ctx1);

	if (manual_attach && h_probe) {
		typedef int (*tAttach)(IUnknown*, IUnknown*);
		tAttach attach = (tAttach)GetProcAddress(h_probe, "EfmiProbeAttach");
		say("EfmiProbeAttach -> %d", attach ? attach(s.dev, s.ctx) : -1);
	}

	IDXGIFactory2* factory2 = nullptr;
	factory->QueryInterface(__uuidof(IDXGIFactory2), (void**)&factory2);
	DXGI_SWAP_CHAIN_DESC1 scd = {};
	scd.Width = width; scd.Height = height;
	scd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
	scd.SampleDesc.Count = 1;
	scd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
	scd.BufferCount = 2;
	scd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
	hr = factory2 ? factory2->CreateSwapChainForHwnd(s.dev, hwnd, &scd, nullptr, nullptr, &s.swap) : E_FAIL;
	if (FAILED(hr)) { say("CreateSwapChainForHwnd failed 0x%08x", (unsigned)hr); return 2; }
	say("swap chain %p (vtable in %s)", s.swap, module_of(*(void**)s.swap).c_str());

	if (!build_scene(s)) { say("scene setup failed"); return 3; }
	say("scene ready: heap mesh1 v0@%u v1@%u i@%u (%zu idx), mesh2 v0@%u v1@%u i@%u (%zu idx)",
	    s.m1.off_v0, s.m1.off_v1, s.m1.off_i, s.m1.idx.size(), s.m2.off_v0, s.m2.off_v1, s.m2.off_i, s.m2.idx.size());

	wchar_t ev_name[64];
	swprintf_s(ev_name, L"efmi_sandbox_dump_%lu", GetCurrentProcessId());
	HANDLE ev_dump = CreateEventW(nullptr, FALSE, FALSE, ev_name);
	swprintf_s(ev_name, L"efmi_sandbox_quit_%lu", GetCurrentProcessId());
	HANDLE ev_quit = CreateEventW(nullptr, FALSE, FALSE, ev_name);

	ULONGLONG t0 = GetTickCount64();
	int frame = 0, key_frames = 0;
	ULONGLONG last_beat = t0;
	bool running = true;
	say("READY");
	while (running) {
		MSG msg;
		while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
			if (msg.message == WM_QUIT) running = false;
			TranslateMessage(&msg);
			DispatchMessageW(&msg);
		}
		if (WaitForSingleObject(ev_quit, 0) == WAIT_OBJECT_0) running = false;
		// XXMI samples the keyboard once per Present. Hold the key across exactly
		// one Present and let go before the (slow) analysed frame is rendered:
		// a second key-down while the analysis runs would abort it.
		if (key_frames && --key_frames == 0) {
			press(dump_vk, false);
		} else if (WaitForSingleObject(ev_dump, 0) == WAIT_OBJECT_0 && !key_frames) {
			say("frame %d: pressing vk 0x%x for XXMI", frame, dump_vk);
			press(dump_vk, true);
			key_frames = 1;
		}

		if (!aux.dev && aux_device_after >= 0 && frame >= aux_device_after) {
			if (!aux.create(create, adapter)) return 3;
			say("AUXILIARY READY frame=%d order=after", frame);
		}
		if (g_recreate_mesh3 > 0 && frame && frame % g_recreate_mesh3 == 0 && !recreate_mesh3(s, frame / g_recreate_mesh3, frame))
			return 3;
		float time = animate ? (float)(GetTickCount64() - t0) / 1000.0f : 0.0f;
		render(s, time);
		if (aux.dev && !aux.render(frame)) return 3;
		// What XXMI is about to see when it polls its hotkeys inside Present:
		// the reload key and whether this process has the foreground.
		{
			static int seen_key = -1, seen_fg = -1;
			DWORD fg_pid = 0;
			GetWindowThreadProcessId(GetForegroundWindow(), &fg_pid);
			int key = (GetAsyncKeyState(VK_F10) & 0x8000) != 0, fg = fg_pid == GetCurrentProcessId();
			if (key != seen_key || fg != seen_fg) {
				say("frame %d: F10 %s, foreground %s", frame, key ? "down" : "up", fg ? "ours" : "elsewhere");
				seen_key = key;
				seen_fg = fg;
			}
		}
		s.swap->Present(g_vsync, 0);
		frame++;
		if (GetTickCount64() - last_beat >= 1000) {
			last_beat = GetTickCount64();
			say("beat frame=%d", frame);
		}

		if (max_frames && frame >= max_frames) running = false;
		if (max_seconds && GetTickCount64() - t0 > (ULONGLONG)max_seconds * 1000) running = false;
	}
	if (key_frames) press(dump_vk, false);
	say("exiting after %d frames", frame);
	return 0;
}
