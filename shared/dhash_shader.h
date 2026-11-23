// Shared sampling contract for the external helper and background probe.
#pragma once
static const char shader[] = R"(
Texture2D<float4> tex : register(t0);
SamplerState samp : register(s0);
RWStructuredBuffer<uint> result : register(u0);
[numthreads(1,1,1)] void main() {
 uint lo=0, hi=0;
 for(uint y=0;y<8;y++) for(uint x=0;x<8;x++) {
  float a=dot(tex.SampleLevel(samp,float2((x+.5)/9.,(y+.5)/8.),0),float4(.25,.25,.25,.25));
  float b=dot(tex.SampleLevel(samp,float2((x+1.5)/9.,(y+.5)/8.),0),float4(.25,.25,.25,.25));
  uint i=y*8+x;
  if(a>b) { if(i<32) lo|=1u<<i; else hi|=1u<<(i-32); }
 }
 if((lo|hi)==0) {
  uint4 q=(uint4)floor(saturate(tex.SampleLevel(samp,float2(.5,.5),0))*16.+.5);
  uint4 t=(1u<<q)-1u;
  lo=(t.b<<16)|t.a; hi=(t.r<<16)|t.g;
 }
 result[0]=lo; result[1]=hi;
})";
