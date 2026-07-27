@echo off
setlocal
for %%I in ("%~dp0..\..") do set "PROJECT_ROOT=%%~fI"
cd /d "%PROJECT_ROOT%"
set "NODE=C:\Program Files\nodejs\node.exe"
set "LOCAL_ENTRY=%PROJECT_ROOT%\tools\opencli\runtime\node_modules\@jackwener\opencli\dist\src\main.js"
set "OPENCLI=%APPDATA%\npm\opencli.cmd"

if exist "%NODE%" if exist "%LOCAL_ENTRY%" (
    "%NODE%" "%LOCAL_ENTRY%" doctor
    endlocal
    exit /b %errorlevel%
)

if not exist "%OPENCLI%" (
    echo OpenCLI runtime was not found in the project or global npm directory.
    echo Run: npm install --prefix tools/opencli/runtime @jackwener/opencli@latest
    exit /b 1
)

call "%OPENCLI%" doctor
endlocal
