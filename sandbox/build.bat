@echo off
rem Builds sandbox\build\efmi_sandbox.exe (test program, not part of the product).
setlocal
cd /d "%~dp0"

if not defined VCVARS (
  for /f "usebackq tokens=*" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2^>nul`) do set "VSROOT=%%i"
)
if not defined VCVARS if defined VSROOT set "VCVARS=%VSROOT%\VC\Auxiliary\Build\vcvars64.bat"
if not defined VCVARS set "VCVARS=D:\Microsoft\VC\Auxiliary\Build\vcvars64.bat"
if not exist "%VCVARS%" (
  echo vcvars64.bat not found. Set VCVARS to its full path.
  exit /b 1
)
call "%VCVARS%" >nul 2>&1

if not exist build mkdir build
cl /nologo /std:c++17 /O2 /MT /EHsc /W3 /DUNICODE /D_UNICODE /Fobuild\ src\sandbox.cpp ^
   /Fe:build\efmi_sandbox.exe /link /NOLOGO /SUBSYSTEM:CONSOLE dxgi.lib d3dcompiler.lib user32.lib
if errorlevel 1 exit /b 1
echo --- imports (d3d11.dll must be absent: it is resolved at run time) ---
dumpbin /nologo /dependents build\efmi_sandbox.exe | findstr /i ".dll"
endlocal
