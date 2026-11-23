// Game-external D3D11 sampling of captured bytes. No game context is touched.
// Sampling contract: SpectrumQT/XXMI-Libs-Package DirectX11/DHash64Gpu.cpp.
#include <d3d11.h>
#include <d3dcompiler.h>
#include <wrl/client.h>
#include <cstdint>
#include <cstring>
using Microsoft::WRL::ComPtr;
#include "../../shared/dhash_shader.h"

#define CHECK(x) do { HRESULT hr=(x); if(FAILED(hr)) return hr; } while(0)
extern "C" __declspec(dllexport) HRESULT __cdecl EfmiDHash(
 UINT width, UINT height, UINT format, UINT pitch, const void* bytes, UINT size, uint64_t* hash) {
 if(!width || !height || !bytes || !hash || !pitch || !size) return E_INVALIDARG;
 ComPtr<ID3D11Device> dev; ComPtr<ID3D11DeviceContext> ctx;
 D3D_FEATURE_LEVEL level;
 CHECK(D3D11CreateDevice(nullptr,D3D_DRIVER_TYPE_WARP,nullptr,0,nullptr,0,D3D11_SDK_VERSION,&dev,&level,&ctx));
 UINT support=0; CHECK(dev->CheckFormatSupport((DXGI_FORMAT)format,&support));
 if(!(support&D3D11_FORMAT_SUPPORT_SHADER_SAMPLE)) return E_INVALIDARG;
 // Restrict to normalized/float formats, since the shader samples float4.
 if(format==30 || format==32 || format==3 || format==4) return E_INVALIDARG;
 bool bc=(format>=70 && format<=84)||(format>=94 && format<=99);
 uint64_t rows=bc ? (height+3ull)/4 : height;
 if(rows*pitch>size) return E_INVALIDARG;
 // A small BC mip cannot be the top level of a standalone texture. Place it
 // at mip 2 of a block-aligned allocation without resampling its bytes.
 UINT mip = bc && ((width % 4) || (height % 4)) ? 2 : 0;
 D3D11_TEXTURE2D_DESC td={}; td.Width=width<<mip;td.Height=height<<mip;td.MipLevels=mip+1;td.ArraySize=1;
 td.Format=(DXGI_FORMAT)format;td.SampleDesc.Count=1;td.Usage=D3D11_USAGE_DEFAULT;td.BindFlags=D3D11_BIND_SHADER_RESOURCE;
 ComPtr<ID3D11Texture2D> tex; CHECK(dev->CreateTexture2D(&td,nullptr,&tex));
 ctx->UpdateSubresource(tex.Get(),mip,nullptr,bytes,pitch,0);
 D3D11_SHADER_RESOURCE_VIEW_DESC vd={};vd.Format=(DXGI_FORMAT)format;vd.ViewDimension=D3D11_SRV_DIMENSION_TEXTURE2D;
 vd.Texture2D.MostDetailedMip=mip;vd.Texture2D.MipLevels=1;
 ComPtr<ID3D11ShaderResourceView> srv; CHECK(dev->CreateShaderResourceView(tex.Get(),&vd,&srv));
 ComPtr<ID3DBlob> code,errors; CHECK(D3DCompile(shader,sizeof(shader),nullptr,nullptr,nullptr,"main","cs_5_0",D3DCOMPILE_ENABLE_STRICTNESS,0,&code,&errors));
 ComPtr<ID3D11ComputeShader> cs; CHECK(dev->CreateComputeShader(code->GetBufferPointer(),code->GetBufferSize(),nullptr,&cs));
 D3D11_SAMPLER_DESC sd={};sd.Filter=D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
 sd.AddressU=sd.AddressV=sd.AddressW=D3D11_TEXTURE_ADDRESS_CLAMP;sd.ComparisonFunc=D3D11_COMPARISON_ALWAYS;
 ComPtr<ID3D11SamplerState> sampler;CHECK(dev->CreateSamplerState(&sd,&sampler));
 D3D11_BUFFER_DESC bd={};bd.ByteWidth=8;bd.BindFlags=D3D11_BIND_UNORDERED_ACCESS;bd.MiscFlags=D3D11_RESOURCE_MISC_BUFFER_STRUCTURED;bd.StructureByteStride=4;
 ComPtr<ID3D11Buffer> out,staging;CHECK(dev->CreateBuffer(&bd,nullptr,&out));
 D3D11_UNORDERED_ACCESS_VIEW_DESC ud={};ud.ViewDimension=D3D11_UAV_DIMENSION_BUFFER;ud.Buffer.NumElements=2;
 ComPtr<ID3D11UnorderedAccessView> uav;CHECK(dev->CreateUnorderedAccessView(out.Get(),&ud,&uav));
 bd.BindFlags=bd.MiscFlags=bd.StructureByteStride=0;bd.Usage=D3D11_USAGE_STAGING;bd.CPUAccessFlags=D3D11_CPU_ACCESS_READ;
 CHECK(dev->CreateBuffer(&bd,nullptr,&staging));
 ctx->CSSetShader(cs.Get(),nullptr,0);ctx->CSSetShaderResources(0,1,srv.GetAddressOf());
 ctx->CSSetSamplers(0,1,sampler.GetAddressOf());ctx->CSSetUnorderedAccessViews(0,1,uav.GetAddressOf(),nullptr);
 ctx->Dispatch(1,1,1);ctx->CopyResource(staging.Get(),out.Get());
 D3D11_MAPPED_SUBRESOURCE mapped={};CHECK(ctx->Map(staging.Get(),0,D3D11_MAP_READ,0,&mapped));
 memcpy(hash,mapped.pData,8);ctx->Unmap(staging.Get(),0);return S_OK;
}
