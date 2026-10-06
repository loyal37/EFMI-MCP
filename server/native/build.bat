@echo off
rem Builds only the optional game-external CRC helper, never efmi_probe.dll.
setlocal
cd /d "%~dp0"
set "CRC_OUT=%~dp0..\..\dist"
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
if not exist "%CRC_OUT%" mkdir "%CRC_OUT%"
cl /nologo /std:c++17 /O2 /MT /W4 /WX /LD crc32c.cpp /Fo"%CRC_OUT%\efmi_crc32c.obj" ^
   /Fe:"%CRC_OUT%\efmi_crc32c.dll" ^
   /link /NOLOGO /OPT:REF /OPT:ICF /IMPLIB:"%CRC_OUT%\efmi_crc32c.lib"
if errorlevel 1 exit /b 1
echo Built %CRC_OUT%\efmi_crc32c.dll
endlocal
