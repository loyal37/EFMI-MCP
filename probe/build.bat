@echo off
rem Builds dist\efmi_probe.dll with the MSVC x64 toolchain.
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
if not exist ..\dist mkdir ..\dist

cl /nologo /std:c++17 /O2 /MT /EHsc /W3 /GS /Zi /DNDEBUG /DUNICODE /D_UNICODE ^
   /Fobuild\ /Fdbuild\efmi_probe_cl.pdb /LD src\*.cpp ^
   /Fe:..\dist\efmi_probe.dll ^
   /link /NOLOGO /DLL /OPT:REF /OPT:ICF /DEBUG /PDB:build\efmi_probe.pdb /IMPLIB:build\efmi_probe.lib ^
   advapi32.lib user32.lib
if errorlevel 1 exit /b 1

echo.
echo --- imports (must not list d3d11.dll or dxgi.dll) ---
dumpbin /nologo /dependents ..\dist\efmi_probe.dll | findstr /i ".dll"
echo --- exports ---
dumpbin /nologo /exports ..\dist\efmi_probe.dll | findstr /i "EfmiProbe"
endlocal
